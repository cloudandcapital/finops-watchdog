"""Valid service names and a zero baseline must survive the real CCAC boundary."""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from ccac import validate_document
from finops_lite.billing import build_billing_result as bill

from finops_watchdog.billing import build_billing_result
from finops_watchdog.ccac import DetectorConfig, build_result

IDENTITY = dict(
    run_id="7e5f2340-60aa-46e6-a7dd-66ed33854d03", generated_at="2026-10-24T00:00:00Z"
)


def daily(service, baseline="100", observed="250", currency="USD"):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ChargePeriodStart",
            "ChargePeriodEnd",
            "ServiceName",
            "BilledCost",
            "BillingCurrency",
            "ProviderName",
            "BillingPeriodStart",
            "BillingPeriodEnd",
        ]
    )
    for day in range(1, 31):
        writer.writerow(
            [
                date(2026, 9, day),
                date(2026, 9, day) + timedelta(days=1),
                service,
                observed if day == 21 else baseline,
                currency,
                "AWS",
                "2026-09-01",
                "2026-10-01",
            ]
        )
    return bill([dict(name="daily.csv", text=output.getvalue())], **IDENTITY)


@pytest.mark.parametrize(
    "service",
    [
        "Amazon EC2",
        "Amazon Elastic Compute Cloud",
        "Amazon CloudFront",
        "Amazon Route 53",
        "Amazon Simple Storage Service",
        "Amazon DynamoDB",
        "Lambda",
        "AWS Lambda",
        "Compute, storage / east",
        "Żółć storage",
        "💚 Cloud",
    ],
)
def test_valid_service_name_produces_schema_valid_stable_spike(service):
    source = daily(service)
    result = build_billing_result(source, generated_at=IDENTITY["generated_at"])
    assert validate_document(result) == []
    finding = result["findings"][0]
    assert finding["title"] == f"{service} spending spike on Sep 21, 2026"
    assert finding["first_observed_at"] == "2026-09-21T00:00:00Z"
    assert "cost $250.00" in finding["description"]
    assert (
        finding["id"]
        == build_billing_result(source, generated_at=IDENTITY["generated_at"])[
            "findings"
        ][0]["id"]
    )
    assert result["extensions"]["finops_watchdog"]["anomalies_detected"] == 1


def test_zero_baseline_is_new_spending_and_existing_metrics_do_not_change():
    source = daily("S3", baseline="0", observed="50.000000000000000001")
    original = build_result(
        source,
        config=DetectorConfig(),
        generated_at=IDENTITY["generated_at"],
        contract_version="1.1.0",
        expected_run_id=source["run_id"],
        expected_mode=source["mode"],
    )
    result = build_billing_result(source, generated_at=IDENTITY["generated_at"])
    assert validate_document(result) == []
    finding = result["findings"][0]
    assert finding["title"] == "New S3 spending on Sep 21, 2026"
    assert "drop" not in finding["description"]
    assert "cost $50.00" in finding["description"]
    assert "median was $0.00" in finding["description"]
    observed = next(
        item for item in result["metrics"] if item["id"].endswith(".observed")
    )
    assert observed["dimensions"]["anomaly_type"] == "new_spend"
    assert result["metrics"] == original["metrics"]
    assert (
        source["extensions"]["finops_lite"]["billing"]["current"]["rows"][20]["cost"]
        == "50.000000000000000001"
    )
    assert not result["opportunities"]


@pytest.mark.parametrize(
    "currency,amount,display",
    [
        ("JPY", "250", "¥250"),
        ("KWD", "250.001", "KWD\u00a0250.000"),
        ("EUR", "250.125", "€250.13"),
    ],
)
def test_billing_money_formats_existing_diagnostic_values_without_changing_them(
    currency, amount, display
):
    source = daily("Amazon EC2", currency=currency, observed=amount)
    original = build_result(
        source,
        config=DetectorConfig(),
        generated_at=IDENTITY["generated_at"],
        contract_version="1.1.0",
        expected_run_id=source["run_id"],
        expected_mode=source["mode"],
    )
    result = build_billing_result(source, generated_at=IDENTITY["generated_at"])
    assert validate_document(result) == []
    assert f"cost {display}" in result["findings"][0]["description"]
    assert result["metrics"] == original["metrics"]
    assert Decimal(
        source["extensions"]["finops_lite"]["billing"]["current"]["rows"][20]["cost"]
    ) == Decimal(amount)
