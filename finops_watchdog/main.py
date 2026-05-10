#!/usr/bin/env python3
"""FinOps Watchdog v0.1 CLI."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import click
import pandas as pd
import yaml

SCHEMA_VERSION = "1.0"


class InputFileError(Exception):
    """Raised when the input file cannot be opened or read."""


class SchemaDataError(Exception):
    """Raised when CSV schema or data is invalid."""


@dataclass(frozen=True)
class DetectConfig:
    """Runtime configuration for a detect invocation."""

    input_path: Path
    time_column: str
    value_column: str
    group_by: str
    window: str
    window_days: int
    threshold: float
    min_amount: float
    output_format: str
    report_path: Path | None = None


@click.group()
def cli() -> None:
    """FinOps Watchdog v0.1."""


def _window_callback(_: click.Context, __: click.Option, value: str) -> str:
    try:
        _parse_window_days(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    return value


@cli.command()
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Path to input CSV file.",
)
@click.option("--time-column", required=True, help="Timestamp column name.")
@click.option("--value-column", required=True, help="Numeric cost column name.")
@click.option("--group-by", required=True, help="Grouping column name.")
@click.option(
    "--output-format",
    type=click.Choice(["json", "csv", "yaml"], case_sensitive=False),
    required=True,
    help="Output format.",
)
@click.option(
    "--window",
    default="30d",
    show_default=True,
    callback=_window_callback,
    help="Lookback window in days (for example: 30d).",
)
@click.option(
    "--threshold",
    type=click.FloatRange(min=0.0, min_open=True),
    default=3.0,
    show_default=True,
    help="Anomaly threshold measured in standard deviations above baseline.",
)
@click.option(
    "--min-amount",
    type=click.FloatRange(min=0.0),
    default=0.0,
    show_default=True,
    help="Ignore anomalies below this absolute delta.",
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path, dir_okay=False, writable=True),
    default=None,
    help="Write a human-readable markdown anomaly summary to this file.",
)
@click.pass_context
def detect(
    ctx: click.Context,
    input_path: Path,
    time_column: str,
    value_column: str,
    group_by: str,
    output_format: str,
    window: str,
    threshold: float,
    min_amount: float,
    report_path: Path | None,
) -> None:
    """Detect spend anomalies from a local CSV file."""

    config = DetectConfig(
        input_path=input_path,
        time_column=time_column,
        value_column=value_column,
        group_by=group_by,
        output_format=output_format.lower(),
        window=window,
        window_days=_parse_window_days(window),
        threshold=threshold,
        min_amount=min_amount,
        report_path=report_path,
    )

    try:
        payload = _run_detection(config)
        _emit_payload(payload, config.output_format)
        if config.report_path is not None:
            _write_markdown_report(payload, config.report_path)
    except InputFileError as exc:
        click.echo(f"input file error: {exc}", err=True)
        ctx.exit(3)
    except SchemaDataError as exc:
        click.echo(f"schema/data error: {exc}", err=True)
        ctx.exit(4)
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        click.echo(f"internal error: {exc}", err=True)
        ctx.exit(5)

    ctx.exit(0)


def _parse_window_days(window: str) -> int:
    match = re.fullmatch(r"([1-9]\d*)d", window.strip().lower())
    if not match:
        raise ValueError("window must match <days>d, for example 30d")
    return int(match.group(1))


def _run_detection(config: DetectConfig) -> Dict[str, Any]:
    data = _load_csv(config.input_path)
    prepared = _prepare_dataframe(
        data,
        time_column=config.time_column,
        value_column=config.value_column,
        group_by=config.group_by,
    )
    anomalies = _detect_anomalies(
        prepared,
        window_days=config.window_days,
        threshold=config.threshold,
        min_amount=config.min_amount,
    )
    return _build_payload(config, anomalies)


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise InputFileError(f"file not found: {path}")
    if not path.is_file():
        raise InputFileError(f"not a file: {path}")

    try:
        return pd.read_csv(path)
    except PermissionError as exc:
        raise InputFileError(f"unreadable file: {path}") from exc
    except FileNotFoundError as exc:
        raise InputFileError(f"file not found: {path}") from exc
    except pd.errors.EmptyDataError as exc:
        raise SchemaDataError("input CSV is empty") from exc
    except Exception as exc:
        raise InputFileError(f"failed to read CSV: {exc}") from exc


def _prepare_dataframe(
    dataframe: pd.DataFrame,
    *,
    time_column: str,
    value_column: str,
    group_by: str,
) -> pd.DataFrame:
    required_columns = [time_column, value_column, group_by]
    missing = [column for column in required_columns if column not in dataframe.columns]
    if missing:
        raise SchemaDataError(f"missing required columns: {', '.join(missing)}")

    prepared = dataframe[[time_column, value_column, group_by]].copy()
    prepared.columns = ["timestamp", "value", "group"]

    prepared["timestamp"] = pd.to_datetime(prepared["timestamp"], errors="coerce", utc=True)
    if prepared["timestamp"].isna().any():
        raise SchemaDataError(f"invalid timestamp values in column '{time_column}'")

    prepared["value"] = pd.to_numeric(prepared["value"], errors="coerce")
    if prepared["value"].isna().any():
        raise SchemaDataError(f"non-numeric values in column '{value_column}'")

    if prepared["group"].isna().any():
        raise SchemaDataError(f"missing group values in column '{group_by}'")

    prepared["group"] = prepared["group"].astype(str)
    prepared = prepared.sort_values(["group", "timestamp"]).reset_index(drop=True)

    return prepared


def _detect_anomalies(
    dataframe: pd.DataFrame,
    *,
    window_days: int,
    threshold: float,
    min_amount: float,
) -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []

    for group_value, group_frame in dataframe.groupby("group", sort=True):
        ordered = group_frame.sort_values("timestamp").copy()

        baseline = ordered["value"].shift(1).rolling(window=window_days, min_periods=window_days).mean()
        rolling_std = ordered["value"].shift(1).rolling(window=window_days, min_periods=window_days).std(ddof=0)

        ordered["baseline"] = baseline
        ordered["rolling_std"] = rolling_std

        for _, row in ordered.iterrows():
            baseline_value = row["baseline"]
            current_value = row["value"]
            std_value = row["rolling_std"]

            if pd.isna(baseline_value) or baseline_value <= 0:
                continue

            delta = current_value - baseline_value
            if delta <= 0 or delta < min_amount:
                continue

            if pd.isna(std_value) or std_value <= 0:
                z_score = float("inf")
            else:
                z_score = delta / std_value

            if z_score < threshold:
                continue

            delta_pct = (delta / baseline_value) * 100.0

            anomalies.append(
                {
                    "timestamp": _to_utc_iso(row["timestamp"]),
                    "group": group_value,
                    "baseline": _rounded_float(baseline_value),
                    "current": _rounded_float(current_value),
                    "delta": _rounded_float(delta),
                    "delta_pct": _rounded_float(delta_pct),
                    "severity": _severity_for_score(z_score, threshold),
                    "anomaly_type": "spend_above_threshold",
                }
            )

    anomalies.sort(key=lambda item: (item["timestamp"], item["group"]))
    return anomalies


def _severity_for_score(z_score: float, threshold: float) -> str:
    if z_score >= threshold * 2.0:
        return "critical"
    if z_score >= threshold * 1.5:
        return "high"
    return "medium"


def _build_payload(config: DetectConfig, anomalies: List[Dict[str, Any]]) -> Dict[str, Any]:
    groups_impacted = len({anomaly["group"] for anomaly in anomalies})
    max_delta_pct = max((abs(anomaly["delta_pct"]) for anomaly in anomalies), default=0.0)

    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "generated_at": _utc_now_iso(),
            "input_file": str(config.input_path),
            "window": config.window,
            "threshold": config.threshold,
            "group_by": config.group_by,
        },
        "summary": {
            "total_anomalies": len(anomalies),
            "groups_impacted": groups_impacted,
            "max_delta_pct": _rounded_float(max_delta_pct),
        },
        "anomalies": anomalies,
    }


def _emit_payload(payload: Dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(payload, indent=2))
        return

    if output_format == "yaml":
        click.echo(yaml.safe_dump(payload, sort_keys=False))
        return

    if output_format == "csv":
        click.echo(_anomalies_to_csv(payload["anomalies"]), nl=False)
        return

    raise ValueError(f"unsupported output format: {output_format}")


def _write_markdown_report(payload: Dict[str, Any], path: Path) -> None:
    meta = payload["metadata"]
    summary = payload["summary"]
    anomalies = payload["anomalies"]

    lines: List[str] = [
        "# FinOps Watchdog — Anomaly Report",
        "",
        f"**Generated:** {meta['generated_at']}  ",
        f"**Input:** `{meta['input_file']}`  ",
        f"**Window:** {meta['window']}  ",
        f"**Threshold:** {meta['threshold']}σ  ",
        f"**Group by:** `{meta['group_by']}`",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total anomalies | {summary['total_anomalies']} |",
        f"| Groups impacted | {summary['groups_impacted']} |",
        f"| Max delta | {summary['max_delta_pct']:.1f}% |",
        "",
    ]

    if not anomalies:
        lines += ["## Anomalies", "", "_No anomalies detected._", ""]
    else:
        lines += [
            "## Anomalies",
            "",
            "| Timestamp | Group | Baseline | Current | Delta | Delta % | Severity |",
            "|-----------|-------|----------|---------|-------|---------|----------|",
        ]
        for a in anomalies:
            lines.append(
                f"| {a['timestamp']} | {a['group']} | {a['baseline']:.2f} |"
                f" {a['current']:.2f} | {a['delta']:.2f} | {a['delta_pct']:.1f}% | **{a['severity']}** |"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _anomalies_to_csv(anomalies: List[Dict[str, Any]]) -> str:
    fieldnames = [
        "timestamp",
        "group",
        "baseline",
        "current",
        "delta",
        "delta_pct",
        "severity",
        "anomaly_type",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for anomaly in anomalies:
        writer.writerow({key: anomaly.get(key) for key in fieldnames})

    return buffer.getvalue()


def _to_utc_iso(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.to_pydatetime().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rounded_float(value: Any) -> float:
    return round(float(value), 4)


if __name__ == "__main__":
    cli()
