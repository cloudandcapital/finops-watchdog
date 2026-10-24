"""Read-only compatibility for FinOps Lite's signed billed-cost CSV profile.

The existing median/MAD detector, thresholds and severity are reused unchanged.
Billing findings name the observed date in plain language. No month charge, missing date or negative day is synthesized
into an observation. Invalid producer lineage fails instead of being omitted.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

from . import __version__


def validate_billing_lineage(payload):
    # Match the billed-cost producer's exact arithmetic budget. The default
    # Decimal context (28 digits) can round a large 18-place source amount.
    # Keep this local so unrelated analyses retain their existing behavior.
    with localcontext() as context:
        context.prec = 50
        return _validate_billing_lineage(payload)


def _validate_billing_lineage(payload):
    from ccac import validate_document

    from .ccac import CCACWatchdogError

    issues = validate_document(payload)
    if issues:
        raise CCACWatchdogError(
            "Billing producer fails CCAC validation: " + issues[0].message
        )
    if (
        payload.get("producer") != {"name": "finops-lite", "version": "0.4.0"}
        or payload.get("contract") != "ccac/1.1.0"
    ):
        raise CCACWatchdogError("Unsupported canonical billing producer.")
    extension = payload.get("extensions", {}).get("finops_lite", {})
    if extension.get("input_profile") != "billing_csv/1.0":
        raise CCACWatchdogError("Unsupported billing input profile.")
    layout = extension.get("billing", {})
    metrics = {item["id"]: item for item in payload["metrics"]}
    scope = metrics.get("metric.tech-spend.scope.cloud", {})
    boundary = scope.get("accounting_boundary", {})
    if any(
        boundary.get(key) != value
        for key, value in {
            "canonical_owner": "finops-lite",
            "scope": "cloud",
            "relationship": "canonical_scope_spend",
            "source_channel": "cloud_provider_billing",
            "cost_basis": "billed_cost",
            "coverage": "partial",
            "total_eligible": False,
        }.items()
    ):
        raise CCACWatchdogError("Billing scope accounting boundary is invalid.")
    source_hashes = {item["id"]: item["content_sha256"] for item in payload["inputs"]}
    for entry in payload["evidence"]:
        if entry.get("content_sha256") not in {
            source_hashes.get(ref) for ref in entry["source_ids"]
        }:
            raise CCACWatchdogError("Billing evidence hash does not match its source.")
    evidence_ids = {item["id"] for item in payload["evidence"]}
    expected_access = (
        ("illustrative_fixture", "public_illustrative")
        if payload["mode"] == "illustrative"
        else ("local_read_only", "customer_confidential")
    )
    if any(
        (item["access"], item["data_classification"]) != expected_access
        for item in payload["inputs"]
    ):
        raise CCACWatchdogError(
            "Billing input privacy classification contradicts its mode."
        )
    declared = layout.get("daily_observation_metric_ids")
    if not isinstance(declared, list) or len(set(declared)) != len(declared):
        raise CCACWatchdogError("Billing daily observation inventory is invalid.")
    candidates = {
        item["id"] for item in metrics.values() if "date" in item["dimensions"]
    }
    if set(declared) != candidates:
        raise CCACWatchdogError(
            "Billing daily metric inventory does not match the producer."
        )
    rows = layout.get("current", {}).get("rows", [])
    eligible_by_series = {}
    for row in rows:
        key = (row["provider"], row["service"])
        eligible_by_series.setdefault(key, []).append(row)
    verified_ids = set()
    for key, records in eligible_by_series.items():
        # Reconstruct the producer's in-window daily evidence independently.
        # Late charges stay in bill totals, but cannot bridge a daily-series gap.
        records = [
            row
            for row in records
            if not (
                row.get("billingStart")
                and (
                    datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
                    <= datetime.fromisoformat(row["billingStart"] + "T00:00:00+00:00")
                    or datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
                    >= datetime.fromisoformat(row["billingEnd"] + "T00:00:00+00:00")
                )
            )
        ]
        by_day = {}
        full_days = True
        for row in records:
            start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
            full_days &= (
                start.time() == datetime.min.time() and end == start + timedelta(days=1)
            )
            by_day[str(start.date())] = by_day.get(
                str(start.date()), Decimal(0)
            ) + Decimal(row["cost"])
        dates = sorted(by_day)
        consecutive = all(
            date.fromisoformat(b) == date.fromisoformat(a) + timedelta(days=1)
            for a, b in zip(dates, dates[1:])
        )
        eligible = (
            bool(records)
            and full_days
            and consecutive
            and all(value >= 0 for value in by_day.values())
        )
        matches = [
            item
            for item in metrics.values()
            if item["id"] in candidates
            and (item["dimensions"].get("provider"), item["dimensions"].get("service"))
            == key
        ]
        if not eligible and matches:
            raise CCACWatchdogError(
                "Ineligible billing records were presented as daily observations."
            )
        if eligible:
            if len(matches) != len(by_day):
                raise CCACWatchdogError(
                    "Eligible billing series has missing daily metrics."
                )
            for item in matches:
                dims = item["dimensions"]
                value_date = dims.get("date")
                if (
                    set(dims) != {"scope", "provider", "service", "date"}
                    or dims.get("scope") != "cloud"
                    or item["basis"] != "observed"
                    or item["additivity"] != "additive"
                    or item["unit"] != "currency"
                    or item["currency"] != scope["currency"]
                    or item["period"]
                    != {
                        "start": value_date,
                        "end": str(date.fromisoformat(value_date) + timedelta(days=1)),
                        "timezone": "UTC",
                    }
                    or Decimal(str(item["value"])) != by_day.get(value_date)
                    or not set(item["evidence_ids"]).issubset(evidence_ids)
                ):
                    raise CCACWatchdogError(
                        "Billing daily observation differs from its source records."
                    )
                verified_ids.add(item["id"])
    if verified_ids != set(declared):
        raise CCACWatchdogError(
            "Billing observation contains an unknown service or date."
        )
    return layout


def build_billing_result(payload, *, config=None, generated_at=None):
    from finops_lite.billing import text_decimal
    from finops_lite.ccac_json import dumps_exact

    from .ccac import DetectorConfig, _timestamp, build_result

    layout = validate_billing_lineage(payload)
    config = config or DetectorConfig()
    config.validate()
    generated = _timestamp(generated_at)
    if layout["daily_observation_metric_ids"]:
        result = build_result(
            payload,
            config=config,
            generated_at=generated,
            contract_version="1.1.0",
            expected_run_id=payload["run_id"],
            expected_mode=payload["mode"],
        )
    else:
        digest = hashlib.sha256(dumps_exact(payload).encode()).hexdigest()
        source_id = "source.finops-watchdog.finops-lite-result"
        result = dict(
            contract="ccac/1.1.0",
            document_type="tool_result",
            producer=dict(name="finops-watchdog", version=__version__),
            run_id=payload["run_id"],
            generated_at=generated,
            mode=payload["mode"],
            period=deepcopy(payload["period"]),
            inputs=[
                dict(
                    id=source_id,
                    source_type="ccac_tool_result",
                    source_version="ccac/1.1.0",
                    adapter_version=__version__,
                    content_sha256=digest,
                    access=(
                        "illustrative_fixture"
                        if payload["mode"] == "illustrative"
                        else "local_read_only"
                    ),
                    data_classification=(
                        "public_illustrative"
                        if payload["mode"] == "illustrative"
                        else "customer_confidential"
                    ),
                    lossy_mapping=False,
                    mapping_notes=[
                        "No eligible daily observations; no normality or anomaly conclusion is made."
                    ],
                )
            ],
            quality=dict(status="partial", issues=[]),
            metrics=[],
            findings=[],
            opportunities=[],
            evidence=[
                dict(
                    id="evidence.finops-watchdog.input-result",
                    kind="formula",
                    source_ids=[source_id],
                    description="Daily evidence eligibility review of the supplied FinOps Lite billed-cost result.",
                    locator="ccac:extensions.finops_lite.billing",
                    observed_at=generated,
                    content_sha256=digest,
                )
            ],
            extensions=dict(
                finops_watchdog=dict(
                    detector="trailing_median_mad",
                    window_days=config.window_days,
                    threshold=config.threshold,
                    min_amount=config.min_amount,
                    min_percent=config.min_percent,
                    series_evaluated=0,
                    anomalies_detected=0,
                    insufficient_history_series=[],
                )
            ),
        )
    metrics = {item["id"]: item for item in result["metrics"]}

    def number(item):
        return text_decimal(Decimal(str(item["value"])))

    for finding in result["findings"]:
        own_metrics = [metrics[identifier] for identifier in finding["metric_ids"]]
        observed = next(
            item for item in own_metrics if item["id"].endswith(".observed")
        )
        expected = next(
            item for item in own_metrics if item["id"].endswith(".expected")
        )
        impact = next(item for item in own_metrics if item["id"].endswith(".impact"))
        dimensions = observed["dimensions"]
        observed_day = date.fromisoformat(dimensions["date"])
        date_label = (
            f"{observed_day.strftime('%b')} {observed_day.day}, {observed_day.year}"
        )
        service = dimensions.get("service", "Cloud spending")
        provider = dimensions.get("provider", "Not specified")
        direction = (
            "spike" if dimensions["anomaly_type"] == "spend_increase" else "drop"
        )
        currency = observed["currency"]
        finding["title"] = f"{service} spending {direction} on {date_label}"
        finding["description"] = (
            f"{service} ({provider}) cost {number(observed)} {currency} on {date_label}. "
            f"Its trailing daily median was {number(expected)} {currency}; "
            f"the change from that baseline was {number(impact)} {currency}. "
            "Check the charges for that day with the service owner. "
            "This is an anomaly, not a savings estimate or a proven cause."
        )
    skipped = layout.get("excluded_daily_series", [])
    diagnostic = result["extensions"]["finops_watchdog"]
    diagnostic["series_with_observations"] = diagnostic["series_evaluated"]
    diagnostic["series_evaluated"] -= len(diagnostic["insufficient_history_series"])
    result["extensions"]["finops_watchdog"]["excluded_daily_series"] = deepcopy(skipped)
    result["extensions"]["finops_watchdog"]["input_profile"] = "billing_csv/1.0"
    result["extensions"]["finops_watchdog"]["organizational_coverage"] = "partial"
    result["extensions"]["finops_watchdog"]["total_eligible"] = False
    if skipped or not layout["daily_observation_metric_ids"]:
        result["quality"]["status"] = "partial"
        result["quality"]["issues"].append(
            dict(
                code="billing.daily-evidence.partial",
                severity="warning",
                message=f"{len(skipped)} service series lack eligible consecutive daily billed-cost evidence. No missing day or multi-day charge is treated as observed daily spend.",
            )
        )
    return result
