# Changelog

All notable changes to FinOps Watchdog are documented here.

## [Unreleased]

### Added
- **`--report` flag** — `detect --report <file>` writes a clean human-readable markdown anomaly summary (metadata header, summary table, per-anomaly table with baseline/current/delta/severity) alongside the existing machine-readable JSON/YAML/CSV output without polluting stdout.
- **Pipeline framing** — README rewritten to open with the Visibility → Variance → Tradeoffs system context and cross-links to all four pipeline tools.
- **GitHub Actions CI** — pytest runs on Python 3.10, 3.11, and 3.12 on every push.
- **examples/** — sample cost time-series CSV and expected anomaly output walkthrough.

## [0.1.0] — Initial release

- `detect` command: CSV-in, anomalies-out
- Output formats: `json`, `yaml`, `csv`
- Rolling baseline with configurable window (`--window`) and threshold (`--threshold`)
- Per-group anomaly detection with severity scoring (`medium`, `high`, `critical`)
- Explicit exit codes for automation (0, 2, 3, 4, 5)
- `--min-amount` filter to suppress noise below a dollar threshold
