from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from finops_watchdog.ccac import (
    FINOPS_LITE_1_1_COMMIT,
    FINOPS_LITE_1_1_SHA256,
    LEGACY_DEMO_SHA256,
    CCACWatchdogError,
    DetectorConfig,
    build_result,
    illustrative_input,
)
from finops_watchdog.main import cli


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _diagnostics(payload: dict) -> list[tuple]:
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    return [
        (
            finding["title"],
            finding["severity"],
            tuple(
                (metrics[metric_id]["name"], metrics[metric_id]["value"])
                for metric_id in finding["metric_ids"]
            ),
        )
        for finding in payload["findings"]
    ]


def _build_1_1(source: dict | None = None, **kwargs) -> dict:
    return build_result(
        source or illustrative_input("1.1.0"),
        config=DetectorConfig(),
        generated_at="2026-08-04T12:05:00Z",
        contract_version="1.1.0",
        **kwargs,
    )


def test_default_and_explicit_1_0_are_byte_identical_with_legacy_provenance():
    runner = CliRunner()
    default = runner.invoke(cli, ["ccac", "--demo"])
    explicit = runner.invoke(cli, ["ccac", "--demo", "--contract-version", "1.0.0"])
    assert default.exit_code == explicit.exit_code == 0
    assert default.output == explicit.output
    assert hashlib.sha256(default.output.encode()).hexdigest() == LEGACY_DEMO_SHA256
    payload = json.loads(default.output)
    assert payload["producer"]["version"] == "0.4.0"
    assert payload["inputs"][0]["adapter_version"] == "0.4.0"


@pytest.mark.parametrize("mode", ["illustrative", "real"])
def test_custom_and_real_1_0_use_current_provenance(mode: str):
    source = illustrative_input()
    source["mode"] = mode
    source["metrics"][0]["name"] = "Custom daily cost"
    result = build_result(
        source,
        config=DetectorConfig(),
        generated_at="2026-08-04T12:05:00Z",
    )
    assert result["producer"]["version"] == "0.5.0"
    assert result["inputs"][0]["adapter_version"] == "0.5.0"
    assert result["inputs"][0]["access"] == (
        "illustrative_fixture" if mode == "illustrative" else "local_read_only"
    )


def test_packaged_1_1_is_exact_authoritative_finops_lite_artifact():
    fixture = (
        Path(__file__).parents[1]
        / "finops_watchdog"
        / "data"
        / ("illustrative-finops-lite-1.1.json")
    )
    assert FINOPS_LITE_1_1_COMMIT == "d72649ec07aa57c60a7ea3f8ff2890b8d95c4b93"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == FINOPS_LITE_1_1_SHA256
    payload = illustrative_input("1.1.0")
    canonical = [
        metric
        for metric in payload["metrics"]
        if metric.get("accounting_boundary", {}).get("relationship")
        == "canonical_scope_spend"
    ]
    assert len(canonical) == 1
    assert canonical[0]["id"] == "metric.tech-spend.scope.cloud"
    assert canonical[0]["value"] == 2194.0
    assert canonical[0]["currency"] == "USD"
    assert canonical[0]["accounting_boundary"]["canonical_owner"] == "finops-lite"
    assert canonical[0]["accounting_boundary"]["cost_basis"] == "net_cost"
    assert payload["extensions"]["finops_lite"]["reconciliation"]["status"] == (
        "passed"
    )


def test_explicit_1_1_is_deterministic_and_released_ccac_accepts():
    runner = CliRunner()
    args = ["ccac", "--demo", "--contract-version", "1.1.0"]
    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.output == second.output
    payload = json.loads(first.output)
    assert payload["contract"] == "ccac/1.1.0"
    assert payload["producer"]["version"] == "0.5.0"
    if os.environ.get("REQUIRE_CCAC_RELEASE_VALIDATION") == "1":
        from ccac.validator import validate_document

        assert validate_document(payload) == []


@pytest.mark.parametrize(
    "input_version,output_version",
    [("1.0.0", "1.1.0"), ("1.1.0", "1.0.0")],
)
def test_input_output_contract_mismatch_fails(input_version: str, output_version: str):
    with pytest.raises(CCACWatchdogError, match="input/output contract mismatch"):
        build_result(
            illustrative_input(input_version),
            config=DetectorConfig(),
            contract_version=output_version,
        )


def test_unsupported_contract_and_finops_lite_version_fail():
    source = illustrative_input("1.1.0")
    source["contract"] = "ccac/9.0.0"
    with pytest.raises(CCACWatchdogError, match="unsupported contract"):
        _build_1_1(source)
    with pytest.raises(CCACWatchdogError, match="unsupported output"):
        build_result(
            illustrative_input(),
            config=DetectorConfig(),
            contract_version="9.0.0",
        )
    source = illustrative_input("1.1.0")
    source["producer"]["version"] = "9.9.9"
    with pytest.raises(CCACWatchdogError, match="unsupported finops-lite version"):
        _build_1_1(source)


def test_expected_run_id_and_mode_mismatches_fail_closed():
    source = illustrative_input("1.1.0")
    with pytest.raises(CCACWatchdogError, match="run_id mismatch"):
        _build_1_1(source, expected_run_id="123e4567-e89b-12d3-a456-426614174999")
    with pytest.raises(CCACWatchdogError, match="mode mismatch"):
        _build_1_1(source, expected_mode="real")
    result = _build_1_1(
        source, expected_run_id=source["run_id"], expected_mode=source["mode"]
    )
    assert result["run_id"] == source["run_id"]
    assert result["mode"] == source["mode"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("source_hash", "evidence source hash mismatch"),
        ("unknown_source", "unknown source"),
        ("unknown_evidence", "unknown evidence lineage"),
    ],
)
def test_source_hash_and_evidence_lineage_fail_closed(mutation: str, error: str):
    source = illustrative_input("1.1.0")
    if mutation == "source_hash":
        source["evidence"][0]["content_sha256"] = "f" * 64
    elif mutation == "unknown_source":
        source["evidence"][0]["source_ids"] = ["source.unknown"]
    else:
        daily = next(
            metric for metric in source["metrics"] if "date" in metric["dimensions"]
        )
        daily["evidence_ids"] = ["evidence.unknown"]
    with pytest.raises(CCACWatchdogError, match=error):
        _build_1_1(source)


def test_reporting_period_currency_and_source_lineage_are_preserved():
    source = illustrative_input("1.1.0")
    result = _build_1_1(source)
    assert result["period"] == {
        "start": "2026-07-01",
        "end": "2026-07-22",
        "timezone": "UTC",
    }
    assert {
        metric["currency"] for metric in result["metrics"] if metric["currency"]
    } == {"USD"}
    expected_hash = hashlib.sha256(_canonical(source)).hexdigest()
    assert result["inputs"][0]["content_sha256"] == expected_hash
    assert result["evidence"][0]["content_sha256"] == expected_hash
    assert result["extensions"]["finops_watchdog"]["upstream"] == {
        "producer": {"name": "finops-lite", "version": "0.4.0"},
        "contract": "ccac/1.1.0",
        "content_sha256": expected_hash,
        "evidence_ids": ["evidence.finops-lite.cost-summary"],
        "source_commit": FINOPS_LITE_1_1_COMMIT,
        "artifact_sha256": FINOPS_LITE_1_1_SHA256,
    }


def test_contract_bridge_preserves_anomaly_behavior_and_parent_suppression():
    one_one = illustrative_input("1.1.0")
    one_zero = copy.deepcopy(one_one)
    one_zero["contract"] = "ccac/1.0.0"
    first = build_result(
        one_zero,
        config=DetectorConfig(),
        generated_at="2026-08-04T12:05:00Z",
    )
    second = _build_1_1(one_one)
    assert _diagnostics(first) == _diagnostics(second)
    assert len(second["findings"]) == 2
    assert all("service=" in finding["title"] for finding in second["findings"])


def test_watchdog_1_1_emits_diagnostics_only_and_never_duplicates_cloud_scope():
    result = _build_1_1()
    assert result["opportunities"] == []
    assert result["extensions"]["finops_watchdog"]["organizational_coverage"] == (
        "partial"
    )
    assert result["extensions"]["finops_watchdog"]["total_eligible"] is False
    serialized = json.dumps(result)
    for forbidden in (
        "metric.tech-spend.scope.cloud",
        "metric.tech-spend.scope.direct_ai",
        "metric.tech-spend.scope.saas",
        "canonical_scope_spend",
        "technology_spend_total",
    ):
        assert forbidden not in serialized
    assert all(finding["finding_type"] == "anomaly" for finding in result["findings"])
    assert (
        "savings estimate or root-cause claim" in result["findings"][0]["description"]
    )


@pytest.mark.parametrize("bad_value", [None, "NaN", "Infinity", -1])
def test_invalid_canonical_cloud_scope_financial_value_fails(bad_value):
    source = illustrative_input("1.1.0")
    scope = next(
        metric
        for metric in source["metrics"]
        if metric["id"] == "metric.tech-spend.scope.cloud"
    )
    scope["value"] = bad_value
    with pytest.raises(CCACWatchdogError):
        _build_1_1(source)


def test_missing_or_duplicate_canonical_scope_fails():
    source = illustrative_input("1.1.0")
    scope = next(
        metric
        for metric in source["metrics"]
        if metric["id"] == "metric.tech-spend.scope.cloud"
    )
    source["metrics"].remove(scope)
    with pytest.raises(CCACWatchdogError, match="exactly one"):
        _build_1_1(source)
    source = illustrative_input("1.1.0")
    source["metrics"].append(copy.deepcopy(scope))
    with pytest.raises(CCACWatchdogError, match="exactly one"):
        _build_1_1(source)


@pytest.mark.parametrize("field", ["status", "daily_sum", "service_sum", "difference"])
def test_daily_series_reconciliation_remains_fail_closed(field: str):
    source = illustrative_input("1.1.0")
    reconciliation = source["extensions"]["finops_lite"]["reconciliation"]
    reconciliation[field] = "failed" if field == "status" else 999
    with pytest.raises(CCACWatchdogError, match="daily-series reconciliation"):
        _build_1_1(source)


def test_daily_service_inventory_must_match_actual_metrics():
    source = illustrative_input("1.1.0")
    source["extensions"]["finops_lite"]["daily_service_metric_ids"].pop()
    with pytest.raises(CCACWatchdogError, match="inventory does not reconcile"):
        _build_1_1(source)
