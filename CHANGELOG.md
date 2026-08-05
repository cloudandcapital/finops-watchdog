# Changelog

All notable changes to FinOps Watchdog are documented here.

## [Unreleased]

- Add a standard `finops-watchdog --version` installation smoke check.

## [0.4.0] — Unreleased

Version 0.4.0 is the first release of the CCAC pipeline implementation. The
historical GitHub `v0.3.0` tag predates this implementation, and the package at
that tag internally declared version `0.1.0`. Advancing to `0.4.0` preserves
release chronology without rewriting, moving, or deleting that history. This
version correction does not change analytical behavior.

### Added
- `watchdog ccac` for direct FinOps Lite `ccac/1.0.0` ingestion and CCAC output.
- Trailing median/MAD detection with statistical and financial materiality gates.
- Explicit new-spend and insufficient-history handling.
- Source hashing, inherited run identity and mode, traceable calculated metrics, and lifecycle-ready anomaly findings.
- Deterministic illustrative fixture and adversarial contract tests.
- Opt-in robust median/MAD detection for legacy CSV input and `--fail-on-anomaly` automation behavior.
- Parent-total suppression when reconciled service-level series exist, preventing overlapping anomaly findings.

### Corrected
- Documentation now states the actual CSV flags, defaults, algorithm, mappings, and exit codes.
- Removed claims of automatic provider/FOCUS mapping and a live Cloud Cost Guard integration.
- Clarified that anomaly impact is neither estimated nor verified savings.

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
