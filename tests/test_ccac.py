from __future__ import annotations

import copy
import json

import pytest
from click.testing import CliRunner

from finops_watchdog.ccac import (
    CCACWatchdogError,
    DetectorConfig,
    build_result,
    detect,
    extract_daily_observations,
    illustrative_input,
)
from finops_watchdog.main import cli


def test_demo_is_deterministic_and_emits_anomaly_not_opportunity():
    runner = CliRunner()
    first = runner.invoke(cli, ["ccac", "--demo"])
    second = runner.invoke(cli, ["ccac", "--demo"])
    assert first.exit_code == 0, first.output
    assert first.output == second.output
    payload = json.loads(first.output)
    assert payload["contract"] == "ccac/1.0.0"
    assert payload["producer"] == {"name": "finops-watchdog", "version": "0.4.0"}
    assert payload["mode"] == "illustrative"
    assert payload["run_id"] == illustrative_input()["run_id"]
    assert payload["quality"]["status"] == "valid"
    assert len(payload["findings"]) == 1
    assert payload["findings"][0]["finding_type"] == "anomaly"
    assert payload["findings"][0]["status"] == "open"
    assert payload["opportunities"] == []
    impact = next(
        metric for metric in payload["metrics"] if metric["id"].endswith(".impact")
    )
    assert impact["basis"] == "calculated"
    assert impact["value"] > 0


def test_demo_writes_json_only_to_file(tmp_path):
    target = tmp_path / "watchdog.json"
    result = CliRunner().invoke(cli, ["ccac", "--demo", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert json.loads(target.read_text())["producer"]["name"] == "finops-watchdog"


def test_requires_exactly_one_input_mode():
    runner = CliRunner()
    assert runner.invoke(cli, ["ccac"]).exit_code == 2
    assert runner.invoke(cli, ["ccac", "--demo", "--input", "x.json"]).exit_code == 2


def test_short_series_is_partial_and_never_silently_zeroed():
    source = illustrative_input()
    source["metrics"] = source["metrics"][:5]
    result = build_result(
        source,
        config=DetectorConfig(window_days=14),
        generated_at="2026-08-04T12:05:00Z",
    )
    assert result["quality"]["status"] == "partial"
    assert result["quality"]["issues"][0]["code"] == "insufficient.history"
    assert (
        result["extensions"]["finops_watchdog"]["insufficient_history_series"][0][
            "observations"
        ]
        == 5
    )
    assert result["findings"] == []


@pytest.mark.parametrize(
    "bad_value", [None, "", "not-a-number", "NaN", "Infinity", True, -1]
)
def test_invalid_daily_cost_fails_closed(bad_value):
    source = illustrative_input()
    source["metrics"][0]["value"] = bad_value
    with pytest.raises(CCACWatchdogError):
        build_result(
            source, config=DetectorConfig(), generated_at="2026-08-04T12:05:00Z"
        )


def test_rejects_duplicate_daily_scope():
    source = illustrative_input()
    source["metrics"].append(copy.deepcopy(source["metrics"][0]))
    with pytest.raises(CCACWatchdogError, match="duplicate daily metric"):
        build_result(
            source, config=DetectorConfig(), generated_at="2026-08-04T12:05:00Z"
        )


def test_rejects_missing_date_instead_of_coercing_it_to_zero():
    source = illustrative_input()
    del source["metrics"][5]
    with pytest.raises(CCACWatchdogError, match="missing spend is not coerced to zero"):
        build_result(
            source, config=DetectorConfig(), generated_at="2026-08-04T12:05:00Z"
        )


def test_rejects_currency_changes_within_a_series():
    source = illustrative_input()
    source["metrics"][5]["currency"] = "EUR"
    with pytest.raises(CCACWatchdogError, match="changes currency"):
        build_result(
            source, config=DetectorConfig(), generated_at="2026-08-04T12:05:00Z"
        )


def test_new_spend_is_explicit_and_not_savings():
    source = illustrative_input()
    for metric in source["metrics"][:20]:
        metric["value"] = 0
    source["metrics"][20]["value"] = 50
    result = build_result(
        source, config=DetectorConfig(), generated_at="2026-08-04T12:05:00Z"
    )
    assert len(result["findings"]) == 1
    assert "New Spend" in result["findings"][0]["title"]
    assert "not a savings estimate" in result["findings"][0]["description"]
    assert result["opportunities"] == []
    assert all(
        not metric["id"].endswith(".robust-score") for metric in result["metrics"]
    )


def test_financial_materiality_suppresses_small_spike():
    source = illustrative_input()
    result = build_result(
        source,
        config=DetectorConfig(min_amount=100),
        generated_at="2026-08-04T12:05:00Z",
    )
    assert result["findings"] == []


def test_does_not_invent_service_dimension():
    result = build_result(
        illustrative_input(),
        config=DetectorConfig(),
        generated_at="2026-08-04T12:05:00Z",
    )
    assert all("service" not in metric["dimensions"] for metric in result["metrics"])


def test_reconciled_service_series_suppress_overlapping_parent_total():
    source = illustrative_input()
    children = []
    for original in source["metrics"]:
        for service, share in (("AmazonEC2", 0.7), ("AmazonS3", 0.3)):
            metric = copy.deepcopy(original)
            metric["id"] = metric["id"].replace(
                "metric.cloud.day", f"metric.cloud.service.{service.lower()}.day"
            )
            metric["dimensions"]["service"] = service
            metric["value"] = round(original["value"] * share, 2)
            children.append(metric)
    source["metrics"].extend(children)
    observations = extract_daily_observations(source)
    assert len(observations) == 42
    assert all(
        "service" in dict(observation.dimensions) for observation in observations
    )


def test_rejects_non_finops_lite_or_unknown_contract():
    source = illustrative_input()
    source["producer"]["name"] = "other"
    with pytest.raises(CCACWatchdogError, match="finops-lite"):
        build_result(source, config=DetectorConfig())
    source = illustrative_input()
    source["contract"] = "ccac/2.0.0"
    with pytest.raises(CCACWatchdogError, match="unsupported contract"):
        build_result(source, config=DetectorConfig())


def test_injected_event_precision_and_recall_are_one():
    source = illustrative_input()
    metrics = source["metrics"]
    second = copy.deepcopy(metrics)
    for metric in second:
        metric["id"] = metric["id"].replace(
            "metric.cloud.day", "metric.cloud.service.s3.day"
        )
        metric["dimensions"]["service"] = "AmazonS3"
        metric["value"] = 40
    second[-1]["value"] = 90
    third = copy.deepcopy(metrics)
    for metric in third:
        metric["id"] = metric["id"].replace(
            "metric.cloud.day", "metric.cloud.service.rds.day"
        )
        metric["dimensions"]["service"] = "AmazonRDS"
        metric["value"] = 60
    source["metrics"] = metrics + second + third

    events, insufficient = detect(extract_daily_observations(source), DetectorConfig())
    predicted = {
        (
            event["observation"].day.isoformat(),
            dict(event["observation"].dimensions).get("service", "AWS-total"),
        )
        for event in events
    }
    expected = {("2026-07-21", "AmazonS3")}
    true_positives = len(predicted & expected)
    precision = true_positives / len(predicted)
    recall = true_positives / len(expected)
    assert insufficient == []
    assert precision == 1.0
    assert recall == 1.0
