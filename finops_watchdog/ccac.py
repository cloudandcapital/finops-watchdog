"""CCAC ingestion and robust cost anomaly detection."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__

CONTRACTS = {"1.0.0": "ccac/1.0.0", "1.1.0": "ccac/1.1.0"}
CONTRACT = CONTRACTS["1.0.0"]
LEGACY_VERSION = "0.4.0"
LEGACY_DEMO_SHA256 = "5f2b954062a3bcf709df6f0f709348f67ee90029bc06606886931179082689b5"
FINOPS_LITE_1_1_COMMIT = "d72649ec07aa57c60a7ea3f8ff2890b8d95c4b93"
FINOPS_LITE_1_1_SHA256 = (
    "ae40d79949a0f6abccaf0f810e602eae8649b02c9a1379af405af2a1fc97b3ac"
)
SUPPORTED_FINOPS_LITE_VERSIONS = {
    "ccac/1.0.0": {"0.3.0", "0.4.0"},
    "ccac/1.1.0": {"0.4.0"},
}
MONEY = Decimal("0.01")


class CCACWatchdogError(ValueError):
    """Raised when input cannot support a trustworthy Watchdog result."""


@dataclass(frozen=True)
class DetectorConfig:
    window_days: int = 14
    threshold: float = 3.0
    min_amount: float = 10.0
    min_percent: float = 20.0

    def validate(self) -> None:
        if self.window_days < 2:
            raise CCACWatchdogError("window_days must be at least 2")
        for name, value in (
            ("threshold", self.threshold),
            ("min_amount", self.min_amount),
            ("min_percent", self.min_percent),
        ):
            if not math.isfinite(value) or value < 0:
                raise CCACWatchdogError(f"{name} must be a finite non-negative number")
        if self.threshold == 0:
            raise CCACWatchdogError("threshold must be greater than zero")


@dataclass(frozen=True)
class Observation:
    metric_id: str
    day: date
    value: Decimal
    currency: str
    dimensions: tuple[tuple[str, str], ...]
    evidence_ids: tuple[str, ...]


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool) or value == "":
        raise CCACWatchdogError(f"missing numeric value for {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CCACWatchdogError(
            f"invalid numeric value for {field}: {value!r}"
        ) from exc
    if not result.is_finite():
        raise CCACWatchdogError(f"non-finite numeric value for {field}: {value!r}")
    return result


def _money(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _number(value: Decimal | float) -> float:
    return round(float(value), 4)


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CCACWatchdogError(f"invalid RFC3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "all"
    return f"{clean[:40]}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def load_ccac(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CCACWatchdogError(f"input file not found: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CCACWatchdogError(f"unable to read JSON input: {exc}") from exc
    if not isinstance(payload, dict):
        raise CCACWatchdogError("input JSON must be an object")
    return payload


def _validate_envelope(
    payload: Mapping[str, Any],
    expected_contract: str,
    *,
    expected_run_id: str | None = None,
    expected_mode: str | None = None,
) -> None:
    contract = payload.get("contract")
    if contract not in CONTRACTS.values():
        raise CCACWatchdogError(
            f"unsupported contract: {contract!r}; supported contracts are ccac/1.0.0 and ccac/1.1.0"
        )
    if contract != expected_contract:
        raise CCACWatchdogError(
            f"input/output contract mismatch: input is {contract}; requested output is {expected_contract}"
        )
    if payload.get("document_type") != "tool_result":
        raise CCACWatchdogError("input document_type must be 'tool_result'")
    producer = payload.get("producer")
    if not isinstance(producer, Mapping) or producer.get("name") != "finops-lite":
        raise CCACWatchdogError("CCAC input must be produced by finops-lite")
    producer_version = producer.get("version")
    if producer_version not in SUPPORTED_FINOPS_LITE_VERSIONS[expected_contract]:
        raise CCACWatchdogError(
            f"unsupported finops-lite version {producer_version!r} for {expected_contract}"
        )
    mode = payload.get("mode")
    if mode not in {"illustrative", "real"}:
        raise CCACWatchdogError("input mode must be illustrative or real")
    if expected_mode is not None and mode != expected_mode:
        raise CCACWatchdogError(
            f"input mode mismatch: input is {mode}; expected {expected_mode}"
        )
    try:
        run_id = str(payload.get("run_id"))
        uuid.UUID(run_id)
    except (ValueError, TypeError) as exc:
        raise CCACWatchdogError("input run_id must be a UUID") from exc
    if expected_run_id is not None and run_id != expected_run_id:
        raise CCACWatchdogError(
            f"input run_id mismatch: input is {run_id}; expected {expected_run_id}"
        )
    if not isinstance(payload.get("metrics"), list):
        raise CCACWatchdogError("input metrics must be an array")
    period = payload.get("period")
    if not isinstance(period, Mapping):
        raise CCACWatchdogError("input period is required")
    try:
        start = date.fromisoformat(str(period.get("start")))
        end = date.fromisoformat(str(period.get("end")))
    except ValueError as exc:
        raise CCACWatchdogError("input period must use ISO dates") from exc
    if end <= start or period.get("timezone") != "UTC":
        raise CCACWatchdogError("input period must be half-open with timezone UTC")
    if expected_contract == CONTRACTS["1.1.0"]:
        _validate_1_1_lineage(payload)


def _validate_1_1_lineage(payload: Mapping[str, Any]) -> None:
    inputs = payload.get("inputs")
    evidence = payload.get("evidence")
    if not isinstance(inputs, list) or not inputs:
        raise CCACWatchdogError("CCAC 1.1 input sources are required")
    if not isinstance(evidence, list) or not evidence:
        raise CCACWatchdogError("CCAC 1.1 evidence is required")
    source_hashes: dict[str, str] = {}
    for index, source in enumerate(inputs):
        if not isinstance(source, Mapping):
            raise CCACWatchdogError(f"inputs[{index}] must be an object")
        source_id = str(source.get("id") or "")
        content_hash = str(source.get("content_sha256") or "")
        if not source_id or source_id in source_hashes:
            raise CCACWatchdogError("CCAC 1.1 source identities must be unique")
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise CCACWatchdogError(f"inputs[{index}].content_sha256 is invalid")
        source_hashes[source_id] = content_hash
        expected_access = (
            ("illustrative_fixture", "public_illustrative")
            if payload["mode"] == "illustrative"
            else ("local_read_only", "customer_confidential")
        )
        if (source.get("access"), source.get("data_classification")) != expected_access:
            raise CCACWatchdogError("input mode contradicts source access metadata")
    evidence_ids: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise CCACWatchdogError(f"evidence[{index}] must be an object")
        evidence_id = str(item.get("id") or "")
        if not evidence_id or evidence_id in evidence_ids:
            raise CCACWatchdogError("CCAC 1.1 evidence identities must be unique")
        evidence_ids.add(evidence_id)
        source_ids = item.get("source_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise CCACWatchdogError(f"evidence[{index}].source_ids is required")
        if any(source_id not in source_hashes for source_id in source_ids):
            raise CCACWatchdogError("CCAC 1.1 evidence references an unknown source")
        if item.get("content_sha256") not in {
            source_hashes[source_id] for source_id in source_ids
        }:
            raise CCACWatchdogError("CCAC 1.1 evidence source hash mismatch")
    canonical = []
    for metric in payload["metrics"]:
        if not isinstance(metric, Mapping):
            continue
        boundary = metric.get("accounting_boundary")
        if (
            isinstance(boundary, Mapping)
            and boundary.get("relationship") == "canonical_scope_spend"
        ):
            canonical.append(metric)
    if len(canonical) != 1:
        raise CCACWatchdogError(
            "FinOps Lite CCAC 1.1 input must contain exactly one canonical Cloud scope"
        )
    scope = canonical[0]
    boundary = scope.get("accounting_boundary")
    if not isinstance(boundary, Mapping) or (
        scope.get("id"),
        boundary.get("scope"),
        boundary.get("canonical_owner"),
        boundary.get("source_channel"),
        boundary.get("cost_basis"),
    ) != (
        "metric.tech-spend.scope.cloud",
        "cloud",
        "finops-lite",
        "cloud_provider_billing",
        "net_cost",
    ):
        raise CCACWatchdogError("FinOps Lite CCAC 1.1 canonical Cloud scope is invalid")
    value = _decimal(scope.get("value"), "canonical Cloud scope value")
    if value < 0:
        raise CCACWatchdogError("canonical Cloud scope value cannot be negative")
    if scope.get("currency") != "USD":
        raise CCACWatchdogError(
            "FinOps Lite CCAC 1.1 canonical Cloud scope must be USD"
        )
    metric_evidence = scope.get("evidence_ids")
    if not isinstance(metric_evidence, list) or any(
        item not in evidence_ids for item in metric_evidence
    ):
        raise CCACWatchdogError("canonical Cloud scope evidence lineage is invalid")
    extensions = payload.get("extensions")
    finops_extension = (
        extensions.get("finops_lite") if isinstance(extensions, Mapping) else None
    )
    reconciliation = (
        finops_extension.get("reconciliation")
        if isinstance(finops_extension, Mapping)
        else None
    )
    if not isinstance(reconciliation, Mapping):
        raise CCACWatchdogError("FinOps Lite daily-series reconciliation is required")
    try:
        total = _decimal(reconciliation.get("total"), "reconciliation.total")
        daily_sum = _decimal(
            reconciliation.get("daily_sum"), "reconciliation.daily_sum"
        )
        service_sum = _decimal(
            reconciliation.get("service_sum"), "reconciliation.service_sum"
        )
        difference = _decimal(
            reconciliation.get("difference"), "reconciliation.difference"
        )
        tolerance = _decimal(
            reconciliation.get("tolerance"), "reconciliation.tolerance"
        )
    except CCACWatchdogError as exc:
        raise CCACWatchdogError(
            f"invalid FinOps Lite daily-series reconciliation: {exc}"
        ) from exc
    if (
        reconciliation.get("status") != "passed"
        or tolerance < 0
        or abs(difference) > tolerance
        or abs(total - value) > tolerance
        or abs(daily_sum - total) > tolerance
        or abs(service_sum - total) > tolerance
    ):
        raise CCACWatchdogError("FinOps Lite daily-series reconciliation failed")
    declared_daily_ids = (
        finops_extension.get("daily_service_metric_ids")
        if isinstance(finops_extension, Mapping)
        else None
    )
    actual_daily_ids = [
        metric.get("id")
        for metric in payload["metrics"]
        if isinstance(metric, Mapping)
        and isinstance(metric.get("dimensions"), Mapping)
        and "date" in metric["dimensions"]
        and "service" in metric["dimensions"]
    ]
    if (
        not isinstance(declared_daily_ids, list)
        or len(declared_daily_ids) != len(set(map(str, declared_daily_ids)))
        or set(map(str, declared_daily_ids)) != set(map(str, actual_daily_ids))
    ):
        raise CCACWatchdogError(
            "FinOps Lite daily-service metric inventory does not reconcile"
        )


def extract_daily_observations(
    payload: Mapping[str, Any],
    expected_contract: str = CONTRACT,
    *,
    expected_run_id: str | None = None,
    expected_mode: str | None = None,
) -> list[Observation]:
    """Extract additive observed one-day currency metrics without inventing dimensions."""
    _validate_envelope(
        payload,
        expected_contract,
        expected_run_id=expected_run_id,
        expected_mode=expected_mode,
    )
    top_period = payload["period"]
    top_start = date.fromisoformat(str(top_period["start"]))
    top_end = date.fromisoformat(str(top_period["end"]))
    known_evidence_ids = {
        str(item.get("id"))
        for item in payload.get("evidence", [])
        if isinstance(item, Mapping)
    }
    observations: list[Observation] = []
    seen: set[tuple[date, tuple[tuple[str, str], ...]]] = set()
    for index, metric in enumerate(payload["metrics"]):
        if not isinstance(metric, Mapping):
            raise CCACWatchdogError(f"metrics[{index}] must be an object")
        dimensions = metric.get("dimensions")
        period = metric.get("period")
        if not isinstance(dimensions, Mapping) or "date" not in dimensions:
            continue
        if (
            metric.get("basis") != "observed"
            or metric.get("unit") != "currency"
            or metric.get("additivity") != "additive"
        ):
            continue
        currency = metric.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise CCACWatchdogError(f"metrics[{index}] has invalid currency")
        if not isinstance(period, Mapping):
            raise CCACWatchdogError(f"metrics[{index}] period is required")
        try:
            day = date.fromisoformat(str(dimensions["date"]))
            start = date.fromisoformat(str(period.get("start")))
            end = date.fromisoformat(str(period.get("end")))
        except ValueError as exc:
            raise CCACWatchdogError(f"metrics[{index}] has invalid date") from exc
        if start != day or end != day + timedelta(days=1):
            raise CCACWatchdogError(f"metrics[{index}] is not a one-day metric")
        if day < top_start or end > top_end:
            raise CCACWatchdogError(f"metrics[{index}] falls outside the input period")
        value = _decimal(metric.get("value"), f"metrics[{index}].value")
        if value < 0:
            raise CCACWatchdogError(f"metrics[{index}].value cannot be negative")
        series_dimensions = tuple(
            sorted((str(k), str(v)) for k, v in dimensions.items() if k != "date")
        )
        key = (day, series_dimensions)
        if key in seen:
            raise CCACWatchdogError(
                f"duplicate daily metric for {day} and {dict(series_dimensions)}"
            )
        seen.add(key)
        metric_evidence_ids = metric.get("evidence_ids")
        if not isinstance(metric_evidence_ids, list) or not metric_evidence_ids:
            raise CCACWatchdogError(f"metrics[{index}].evidence_ids is required")
        if expected_contract == CONTRACTS["1.1.0"] and any(
            evidence_id not in known_evidence_ids
            for evidence_id in map(str, metric_evidence_ids)
        ):
            raise CCACWatchdogError(f"metrics[{index}] has unknown evidence lineage")
        observations.append(
            Observation(
                str(metric.get("id")),
                day,
                value,
                currency,
                series_dimensions,
                tuple(map(str, metric_evidence_ids)),
            )
        )
    if not observations:
        raise CCACWatchdogError(
            "input contains no observed additive daily currency metrics"
        )
    service_parent_dimensions = {
        tuple((key, value) for key, value in observation.dimensions if key != "service")
        for observation in observations
        if any(key == "service" for key, _ in observation.dimensions)
    }
    observations = [
        observation
        for observation in observations
        if any(key == "service" for key, _ in observation.dimensions)
        or observation.dimensions not in service_parent_dimensions
    ]
    currencies = {observation.currency for observation in observations}
    if len(currencies) != 1:
        raise CCACWatchdogError("input daily metrics changes currency across series")
    return sorted(observations, key=lambda item: (item.dimensions, item.day))


def _median(values: Sequence[Decimal]) -> Decimal:
    return Decimal(str(statistics.median(values)))


def _severity(score: float | None, delta_pct: Decimal, new_spend: bool) -> str:
    if new_spend or (score is not None and score >= 6) or delta_pct >= 100:
        return "critical"
    if (score is not None and score >= 4.5) or delta_pct >= 50:
        return "high"
    return "medium"


def detect(
    observations: Sequence[Observation], config: DetectorConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return anomaly events and explicit insufficient-history series."""
    config.validate()
    grouped: dict[tuple[tuple[str, str], ...], list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.dimensions, []).append(observation)
    events: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []
    for dimensions, series in sorted(grouped.items()):
        ordered = sorted(series, key=lambda item: item.day)
        currencies = {item.currency for item in ordered}
        if len(currencies) != 1:
            raise CCACWatchdogError(
                f"daily series changes currency for dimensions {dict(dimensions)}"
            )
        for previous, current in zip(ordered, ordered[1:]):
            if current.day != previous.day + timedelta(days=1):
                raise CCACWatchdogError(
                    f"daily series has a missing date between {previous.day} and {current.day} for dimensions {dict(dimensions)}; missing spend is not coerced to zero"
                )
        if len(ordered) <= config.window_days:
            insufficient.append(
                {
                    "dimensions": dict(dimensions),
                    "observations": len(ordered),
                    "required": config.window_days + 1,
                }
            )
            continue
        for position in range(config.window_days, len(ordered)):
            current = ordered[position]
            history = ordered[position - config.window_days : position]
            values = [item.value for item in history]
            expected = _median(values)
            deviations = [abs(value - expected) for value in values]
            mad = _median(deviations)
            robust_sigma = mad * Decimal("1.4826")
            delta = current.value - expected
            if delta <= 0 or delta < Decimal(str(config.min_amount)):
                continue
            new_spend = all(value == 0 for value in values) and current.value > 0
            if expected == 0:
                delta_pct = Decimal("100") if new_spend else Decimal("0")
            else:
                delta_pct = delta / expected * Decimal("100")
            if delta_pct < Decimal(str(config.min_percent)):
                continue
            score = None if robust_sigma == 0 else float(delta / robust_sigma)
            statistical_break = (
                new_spend
                or (robust_sigma == 0 and delta > 0)
                or (score is not None and score >= config.threshold)
            )
            if not statistical_break:
                continue
            events.append(
                {
                    "observation": current,
                    "expected": expected,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "mad": mad,
                    "robust_score": score,
                    "anomaly_type": "new_spend" if new_spend else "spend_increase",
                    "severity": _severity(score, delta_pct, new_spend),
                    "history_metric_ids": [item.metric_id for item in history],
                }
            )
    return events, insufficient


def _metric(
    metric_id: str,
    name: str,
    value: Decimal | float | None,
    unit: str,
    currency: str | None,
    period: dict[str, str],
    dimensions: dict[str, str],
    formula: str,
    inputs: list[str],
    evidence_id: str,
) -> dict[str, Any]:
    return {
        "id": metric_id,
        "name": name,
        "value": (
            None
            if value is None
            else (
                _money(value)
                if isinstance(value, Decimal) and unit == "currency"
                else _number(value)
            )
        ),
        "unknown_reason": (
            "Robust score is undefined because the baseline MAD is zero."
            if value is None
            else None
        ),
        "unit": unit,
        "currency": currency,
        "basis": "unknown" if value is None else "calculated",
        "additivity": "non_additive" if unit != "currency" else "additive",
        "period": period,
        "dimensions": dimensions,
        "formula": None if value is None else formula,
        "input_metric_ids": inputs,
        "evidence_ids": [evidence_id],
        "quality_status": "valid",
    }


def build_result(
    payload: Mapping[str, Any],
    *,
    config: DetectorConfig,
    generated_at: str | datetime | None = None,
    contract_version: str = "1.0.0",
    compatibility_demo: bool = False,
    expected_run_id: str | None = None,
    expected_mode: str | None = None,
) -> dict[str, Any]:
    if contract_version not in CONTRACTS:
        raise CCACWatchdogError(
            f"unsupported output contract version: {contract_version!r}"
        )
    output_contract = CONTRACTS[contract_version]
    observations = extract_daily_observations(
        payload,
        output_contract,
        expected_run_id=expected_run_id,
        expected_mode=expected_mode,
    )
    events, insufficient = detect(observations, config)
    generated = _timestamp(generated_at)
    source_bytes = _canonical(payload)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_id = "source.finops-watchdog.finops-lite-result"
    evidence_id = "evidence.finops-watchdog.input-result"
    legacy_demo = (
        compatibility_demo
        and output_contract == CONTRACT
        and _canonical(payload) == _canonical(illustrative_input())
    )
    producer_version = LEGACY_VERSION if legacy_demo else __version__
    metrics: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for event in events:
        obs: Observation = event["observation"]
        dims = dict(obs.dimensions) | {
            "date": obs.day.isoformat(),
            "anomaly_type": event["anomaly_type"],
        }
        component = _slug(
            "|".join(f"{k}={v}" for k, v in obs.dimensions) + f"|{obs.day}"
        )
        prefix = f"metric.anomaly.{component}"
        day_period = {
            "start": obs.day.isoformat(),
            "end": (obs.day + timedelta(days=1)).isoformat(),
            "timezone": "UTC",
        }
        observed_id = f"{prefix}.observed"
        expected_id = f"{prefix}.expected"
        internal_inputs = [observed_id, expected_id]
        event_metrics = [
            _metric(
                observed_id,
                "Observed cost",
                obs.value,
                "currency",
                obs.currency,
                day_period,
                dims,
                "copied from the current observed daily metric",
                [],
                evidence_id,
            ),
            _metric(
                expected_id,
                "Trailing median expected cost",
                event["expected"],
                "currency",
                obs.currency,
                day_period,
                dims,
                f"median of the prior {config.window_days} daily observed costs",
                [],
                evidence_id,
            ),
            _metric(
                f"{prefix}.impact",
                "Observed anomaly cost impact",
                event["delta"],
                "currency",
                obs.currency,
                day_period,
                dims,
                "observed cost - trailing median expected cost",
                internal_inputs,
                evidence_id,
            ),
            _metric(
                f"{prefix}.change-percent",
                "Observed cost change percentage",
                event["delta_pct"],
                "percent",
                None,
                day_period,
                dims,
                "(observed cost - expected cost) / expected cost * 100; new spend is represented as 100",
                internal_inputs,
                evidence_id,
            ),
        ]
        if event["robust_score"] is not None:
            event_metrics.append(
                _metric(
                    f"{prefix}.robust-score",
                    "Robust anomaly score",
                    event["robust_score"],
                    "score",
                    None,
                    day_period,
                    dims,
                    "(observed cost - trailing median) / (1.4826 * median absolute deviation)",
                    internal_inputs,
                    evidence_id,
                )
            )
        event_metrics[0]["basis"] = "observed"
        event_metrics[0]["formula"] = None
        metrics.extend(event_metrics)
        finding_id = f"finding.anomaly.{component}"
        label = (
            ", ".join(f"{key}={value}" for key, value in obs.dimensions)
            or "cloud total"
        )
        findings.append(
            {
                "id": finding_id,
                "finding_type": "anomaly",
                "title": f"{event['anomaly_type'].replace('_', ' ').title()} detected for {label}",
                "description": f"Observed {_money(obs.value)} {obs.currency}; trailing median {_money(event['expected'])} {obs.currency}; observed excess {_money(event['delta'])} {obs.currency}. This is an anomaly impact, not a savings estimate or root-cause claim.",
                "severity": event["severity"],
                "status": "open",
                "metric_ids": [item["id"] for item in event_metrics],
                "evidence_ids": [evidence_id],
                "first_observed_at": f"{obs.day.isoformat()}T00:00:00Z",
                "last_observed_at": f"{obs.day.isoformat()}T23:59:59Z",
            }
        )
    issues = [
        {
            "code": "insufficient.history",
            "severity": "warning",
            "message": f"{item['observations']} daily observations available; {item['required']} required for detection.",
            "source_id": source_id,
            "field": "metrics",
            "row_count": item["observations"],
        }
        for item in insufficient
    ]
    result = {
        "contract": output_contract,
        "document_type": "tool_result",
        "producer": {"name": "finops-watchdog", "version": producer_version},
        "run_id": str(payload["run_id"]),
        "generated_at": generated,
        "mode": payload["mode"],
        "period": dict(payload["period"]),
        "inputs": [
            {
                "id": source_id,
                "source_type": "ccac_tool_result",
                "source_version": output_contract,
                "adapter_version": producer_version,
                "content_sha256": source_hash,
                "access": (
                    "illustrative_fixture"
                    if payload["mode"] == "illustrative"
                    else "local_read_only"
                ),
                "data_classification": (
                    "public_illustrative"
                    if payload["mode"] == "illustrative"
                    else "customer_confidential"
                ),
                "lossy_mapping": False,
                "mapping_notes": [
                    "Consumes observed additive one-day currency metrics; no service attribution is inferred from period totals."
                ],
            }
        ],
        "quality": {"status": "partial" if issues else "valid", "issues": issues},
        "metrics": metrics,
        "findings": findings,
        "opportunities": [],
        "evidence": [
            {
                "id": evidence_id,
                "kind": "formula",
                "source_ids": [source_id],
                "description": "Robust trailing median and MAD analysis of FinOps Lite daily observed metrics.",
                "locator": "ccac:metrics[dimensions.date]",
                "observed_at": generated,
                "content_sha256": source_hash,
            }
        ],
        "extensions": {
            "finops_watchdog": {
                "detector": "trailing_median_mad",
                "window_days": config.window_days,
                "threshold": config.threshold,
                "min_amount": config.min_amount,
                "min_percent": config.min_percent,
                "series_evaluated": len({item.dimensions for item in observations}),
                "anomalies_detected": len(events),
                "insufficient_history_series": insufficient,
                "lifecycle_default": "open",
            }
        },
    }
    if output_contract == CONTRACTS["1.1.0"]:
        result["extensions"]["finops_watchdog"]["upstream"] = {
            "producer": dict(payload["producer"]),
            "contract": str(payload["contract"]),
            "content_sha256": source_hash,
            "evidence_ids": sorted(
                str(item["id"])
                for item in payload["evidence"]
                if isinstance(item, Mapping) and item.get("id")
            ),
        }
        result["extensions"]["finops_watchdog"].update(
            {"organizational_coverage": "partial", "total_eligible": False}
        )
        if _canonical(payload) == _canonical(illustrative_input("1.1.0")):
            result["extensions"]["finops_watchdog"]["upstream"].update(
                {
                    "source_commit": FINOPS_LITE_1_1_COMMIT,
                    "artifact_sha256": FINOPS_LITE_1_1_SHA256,
                }
            )
    return result


def illustrative_input(contract_version: str = "1.0.0") -> dict[str, Any]:
    """Deterministic FinOps Lite-shaped fixture with one injected spike."""
    if contract_version == "1.1.0":
        resource = files("finops_watchdog").joinpath(
            "data/illustrative-finops-lite-1.1.json"
        )
        raw = resource.read_bytes()
        if hashlib.sha256(raw).hexdigest() != FINOPS_LITE_1_1_SHA256:
            raise CCACWatchdogError("packaged FinOps Lite 1.1 fixture hash mismatch")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CCACWatchdogError("packaged FinOps Lite 1.1 fixture is invalid")
        return value
    if contract_version != "1.0.0":
        raise CCACWatchdogError(
            f"unsupported illustrative contract version: {contract_version!r}"
        )
    start = date(2026, 7, 1)
    values = [Decimal("100") + Decimal(index % 3) for index in range(20)] + [
        Decimal("175")
    ]
    evidence_id = "evidence.finops-lite.demo"
    metrics = []
    for index, value in enumerate(values):
        day = start + timedelta(days=index)
        metrics.append(
            {
                "id": f"metric.cloud.day.{day}.cost",
                "name": f"Cloud cost for {day}",
                "value": _money(value),
                "unknown_reason": None,
                "unit": "currency",
                "currency": "USD",
                "basis": "observed",
                "additivity": "additive",
                "period": {
                    "start": str(day),
                    "end": str(day + timedelta(days=1)),
                    "timezone": "UTC",
                },
                "dimensions": {"scope": "cloud", "provider": "aws", "date": str(day)},
                "formula": None,
                "input_metric_ids": [],
                "evidence_ids": [evidence_id],
                "quality_status": "valid",
            }
        )
    period = {
        "start": str(start),
        "end": str(start + timedelta(days=len(values))),
        "timezone": "UTC",
    }
    return {
        "contract": CONTRACT,
        "document_type": "tool_result",
        "producer": {"name": "finops-lite", "version": "0.3.0"},
        "run_id": "123e4567-e89b-12d3-a456-426614174010",
        "generated_at": "2026-08-04T12:00:00Z",
        "mode": "illustrative",
        "period": period,
        "inputs": [
            {
                "id": "source.finops-lite.demo",
                "source_type": "fixture",
                "source_version": "1.0",
                "content_sha256": "0" * 64,
                "access": "illustrative_fixture",
                "data_classification": "public_illustrative",
            }
        ],
        "quality": {"status": "valid", "issues": []},
        "metrics": metrics,
        "findings": [],
        "opportunities": [],
        "evidence": [
            {
                "id": evidence_id,
                "kind": "source_query",
                "source_ids": ["source.finops-lite.demo"],
                "description": "Illustrative daily AWS cost fixture.",
            }
        ],
        "extensions": {},
    }
