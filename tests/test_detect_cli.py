"""Contract tests for the v0.1 CSV-only detect command."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from finops_watchdog.main import cli

FIXTURES = Path(__file__).parent / "fixtures"


def test_version_option() -> None:
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "finops-watchdog, version 0.2.0"


def _detect_args(input_name: str) -> list[str]:
    return [
        "detect",
        "--input",
        str(FIXTURES / input_name),
        "--time-column",
        "date",
        "--value-column",
        "amount",
        "--group-by",
        "SERVICE",
        "--window",
        "5d",
        "--threshold",
        "3.0",
        "--min-amount",
        "1",
        "--output-format",
        "json",
    ]


def test_detect_no_anomaly_flat_series() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, _detect_args("no_anomaly.csv"))

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["summary"]["total_anomalies"] == 0
    assert payload["summary"]["groups_impacted"] == 0
    assert payload["anomalies"] == []


def test_detect_simple_spike() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, _detect_args("simple_spike.csv"))

    assert result.exit_code == 0
    payload = json.loads(result.output)

    assert payload["summary"]["total_anomalies"] >= 1
    anomaly = payload["anomalies"][0]
    assert anomaly["group"] == "AmazonEC2"
    assert anomaly["anomaly_type"] == "spend_above_threshold"
    assert anomaly["delta"] > 0
    assert anomaly["delta_pct"] > 0


def test_detect_multi_group_grouped() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, _detect_args("multi_group.csv"))

    assert result.exit_code == 0
    payload = json.loads(result.output)

    assert payload["summary"]["total_anomalies"] >= 1
    groups = {entry["group"] for entry in payload["anomalies"]}
    assert "AmazonEC2" in groups
    assert "AmazonS3" not in groups
    assert payload["summary"]["groups_impacted"] == len(groups)


def test_detect_missing_input_file_returns_exit3() -> None:
    runner = CliRunner()

    args = _detect_args("does_not_exist.csv")
    result = runner.invoke(cli, args)

    assert result.exit_code == 3
    assert "input file error" in result.output.lower()


def test_detect_missing_value_column_returns_exit4() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, _detect_args("missing_column.csv"))

    assert result.exit_code == 4
    assert "schema/data error" in result.output.lower()


def test_detect_non_numeric_value_returns_exit4() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, _detect_args("non_numeric.csv"))

    assert result.exit_code == 4
    assert "schema/data error" in result.output.lower()


def test_detect_requires_input_and_column_flags_returns_exit2() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["detect"])

    assert result.exit_code == 2


def test_detect_json_mode_stdout_is_valid_json_only() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, _detect_args("simple_spike.csv"))

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["schema_version"] == "1.0"
    assert result.output.strip().startswith("{")
    assert result.output.strip().endswith("}")


def test_robust_csv_algorithm_and_opt_in_anomaly_exit():
    runner = CliRunner()
    args = _detect_args("simple_spike.csv") + [
        "--algorithm",
        "robust",
        "--min-percent",
        "20",
        "--fail-on-anomaly",
    ]
    result = runner.invoke(cli, args)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["metadata"]["algorithm"] == "robust"
    assert payload["anomalies"][0]["anomaly_type"] == "spend_above_robust_threshold"


def test_fail_on_anomaly_keeps_no_anomaly_successful():
    result = CliRunner().invoke(
        cli, _detect_args("no_anomaly.csv") + ["--fail-on-anomaly"]
    )
    assert result.exit_code == 0
