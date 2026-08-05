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
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__

CONTRACT = "ccac/1.0.0"
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


def _validate_envelope(payload: Mapping[str, Any]) -> None:
    if payload.get("contract") != CONTRACT:
        raise CCACWatchdogError(
            f"unsupported contract: {payload.get('contract')!r}; expected {CONTRACT}"
        )
    if payload.get("document_type") != "tool_result":
        raise CCACWatchdogError("input document_type must be 'tool_result'")
    producer = payload.get("producer")
    if not isinstance(producer, Mapping) or producer.get("name") != "finops-lite":
        raise CCACWatchdogError("CCAC input must be produced by finops-lite")
    if payload.get("mode") not in {"illustrative", "real"}:
        raise CCACWatchdogError("input mode must be illustrative or real")
    try:
        uuid.UUID(str(payload.get("run_id")))
    except (ValueError, TypeError) as exc:
        raise CCACWatchdogError("input run_id must be a UUID") from exc
    if not isinstance(payload.get("metrics"), list):
        raise CCACWatchdogError("input metrics must be an array")
    if not isinstance(payload.get("period"), Mapping):
        raise CCACWatchdogError("input period is required")


def extract_daily_observations(payload: Mapping[str, Any]) -> list[Observation]:
    """Extract additive observed one-day currency metrics without inventing dimensions."""
    _validate_envelope(payload)
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
        evidence_ids = metric.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise CCACWatchdogError(f"metrics[{index}].evidence_ids is required")
        observations.append(
            Observation(
                str(metric.get("id")),
                day,
                value,
                currency,
                series_dimensions,
                tuple(map(str, evidence_ids)),
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
) -> dict[str, Any]:
    observations = extract_daily_observations(payload)
    events, insufficient = detect(observations, config)
    generated = _timestamp(generated_at)
    source_bytes = _canonical(payload)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_id = "source.finops-watchdog.finops-lite-result"
    evidence_id = "evidence.finops-watchdog.input-result"
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
    return {
        "contract": CONTRACT,
        "document_type": "tool_result",
        "producer": {"name": "finops-watchdog", "version": __version__},
        "run_id": str(payload["run_id"]),
        "generated_at": generated,
        "mode": payload["mode"],
        "period": dict(payload["period"]),
        "inputs": [
            {
                "id": source_id,
                "source_type": "ccac_tool_result",
                "source_version": CONTRACT,
                "adapter_version": __version__,
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


def illustrative_input() -> dict[str, Any]:
    """Deterministic FinOps Lite-shaped fixture with one injected spike."""
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
