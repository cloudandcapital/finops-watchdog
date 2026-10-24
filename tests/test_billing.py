"""The billing adapter must reuse the detector and reject invented observations."""

from copy import deepcopy
from decimal import Decimal, getcontext
from pathlib import Path

import pytest
from ccac import validate_document
from finops_lite.billing import build_billing_result as bill
from finops_lite.ccac_json import loads_exact

from finops_watchdog.billing import build_billing_result, validate_billing_lineage
from finops_watchdog.ccac import CCACWatchdogError

IDENTITY = dict(
    run_id="7e5f2340-60aa-46e6-a7dd-66ed33854d03", generated_at="2026-10-20T00:00:00Z"
)


def source(days=21):
    rows = [
        f"2026-09-{index:02d},Shop compute,{250 if index==21 else 100},USD,AWS,day{index}"
        for index in range(1, days + 1)
    ]
    return bill(
        [
            {
                "name": "daily.csv",
                "text": "Date,Service,Cost,Currency,Provider,RecordId\n"
                + "\n".join(rows),
            }
        ],
        **IDENTITY,
    )


def test_existing_median_mad_detector_finds_invented_spike():
    result = build_billing_result(source(), generated_at=IDENTITY["generated_at"])
    diagnostic = result["extensions"]["finops_watchdog"]
    assert diagnostic["detector"] == "trailing_median_mad"
    assert diagnostic["window_days"] == 14 and diagnostic["anomalies_detected"] == 1
    assert len(result["findings"]) == 1
    assert (
        "not a savings estimate or a proven cause"
        in result["findings"][0]["description"]
    )
    assert validate_document(result) == []


def test_short_history_is_unknown_rather_than_no_anomaly_assurance():
    result = build_billing_result(source(3), generated_at=IDENTITY["generated_at"])
    diagnostic = result["extensions"]["finops_watchdog"]
    assert diagnostic["anomalies_detected"] == 0
    assert len(diagnostic["insufficient_history_series"]) == 1
    assert diagnostic["series_evaluated"] == 0
    assert diagnostic["series_with_observations"] == 1
    assert result["quality"]["status"] == "partial"


def test_earlier_correction_does_not_erase_a_current_period_spike():
    from datetime import date, timedelta

    header = "ChargePeriodStart,ChargePeriodEnd,ServiceName,BilledCost,BillingCurrency,BillingPeriodStart,BillingPeriodEnd,ChargeClass\n"
    rows = [
        f"{date(2026,9,d)},{date(2026,9,d)+timedelta(days=1)},EC2,{250 if d==25 else 100},USD,2026-09-01,2026-10-01,Usage"
        for d in range(1, 31)
    ]
    rows.append("2026-08-20,2026-08-21,EC2,-10,USD,2026-09-01,2026-10-01,Correction")
    payload = bill(
        [{"name": "correction.csv", "text": header + "\n".join(rows)}], **IDENTITY
    )
    result = build_billing_result(payload, generated_at=IDENTITY["generated_at"])
    diagnostic = result["extensions"]["finops_watchdog"]
    assert diagnostic["series_evaluated"] == 1
    assert diagnostic["anomalies_detected"] == 1
    finding = result["findings"][0]
    assert finding["title"] == "EC2 spending spike on Sep 25, 2026"
    assert "cost $250.00 on Sep 25, 2026" in finding["description"]
    assert "trailing daily median was $100.00" in finding["description"]
    assert finding["first_observed_at"] == "2026-09-25T00:00:00Z"
    assert any(
        item["dimensions"].get("date") == "2026-09-25" and item["value"] == 250
        for item in result["metrics"]
    )
    assert validate_document(result) == []
    tampered = deepcopy(payload)
    daily = tampered["extensions"]["finops_lite"]["billing"][
        "daily_observation_metric_ids"
    ]
    metric = next(item for item in tampered["metrics"] if item["id"] == daily[0])
    metric["value"] -= Decimal("10")
    with pytest.raises(CCACWatchdogError, match="differs from its source"):
        validate_billing_lineage(tampered)


def test_eighteen_place_source_lineage_is_exact_without_changing_global_context():
    # Invented canonical FinOps Lite output exercises the consumer independently
    # of the installed producer adapter's accepted input precision.
    payload = loads_exact(
        (Path(__file__).parent / "fixtures/billing/precise-source.json").read_text()
    )
    precision = getcontext().prec
    layout = validate_billing_lineage(payload)
    assert layout["current"]["rows"][0]["cost"] == "999999999999.123456789012345678"
    result = build_billing_result(payload, generated_at=IDENTITY["generated_at"])
    assert result["extensions"]["finops_watchdog"]["anomalies_detected"] == 0
    assert getcontext().prec == precision
    # A one-unit change at the eighteenth place must still fail lineage.
    target = next(
        item
        for item in payload["metrics"]
        if item["id"] == layout["daily_observation_metric_ids"][0]
    )
    target["value"] = Decimal("999999999999.123456789012345680")
    with pytest.raises(CCACWatchdogError, match="differs from its source"):
        validate_billing_lineage(payload)


@pytest.mark.parametrize(
    "body,header",
    [
        ("2026-09-01,Compute,300,USD,2026-10-01", "Date,Service,Cost,Currency,EndDate"),
        (
            "2026-09-01,Compute,10,USD\n2026-09-03,Compute,12,USD",
            "Date,Service,Cost,Currency",
        ),
        ("2026-09-01,Compute,-1,USD", "Date,Service,Cost,Currency"),
    ],
)
def test_months_gaps_and_negative_days_are_excluded_not_filled(body, header):
    payload = bill([{"name": "bill.csv", "text": header + "\n" + body}], **IDENTITY)
    result = build_billing_result(payload, generated_at=IDENTITY["generated_at"])
    assert result["metrics"] == [] and result["findings"] == []
    assert len(result["extensions"]["finops_watchdog"]["excluded_daily_series"]) == 1
    assert result["quality"]["status"] == "partial"
    assert validate_document(result) == []


@pytest.mark.parametrize(
    "mutation", ["value", "date", "inventory", "boundary", "hash", "privacy"]
)
def test_changed_daily_evidence_fails_closed(mutation):
    payload = deepcopy(source())
    layout = payload["extensions"]["finops_lite"]["billing"]
    target = next(
        item
        for item in payload["metrics"]
        if item["id"] == layout["daily_observation_metric_ids"][0]
    )
    if mutation == "value":
        target["value"] = Decimal("999")
    if mutation == "date":
        target["dimensions"]["date"] = "2026-09-30"
    if mutation == "inventory":
        layout["daily_observation_metric_ids"] = []
    if mutation == "boundary":
        next(
            item
            for item in payload["metrics"]
            if item["id"] == "metric.tech-spend.scope.cloud"
        )["accounting_boundary"]["total_eligible"] = True
    if mutation == "hash":
        payload["evidence"][0]["content_sha256"] = "a" * 64
    if mutation == "privacy":
        payload["inputs"][0]["access"] = "illustrative_fixture"
    with pytest.raises(CCACWatchdogError):
        validate_billing_lineage(payload)
