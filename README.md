# FinOps Watchdog

FinOps Watchdog detects financially material increases in daily cloud cost. Version 0.2 adds the canonical Cloud & Capital Analysis Contract (CCAC) path used by the six-tool pipeline. The original explicitly mapped CSV command remains available for compatibility.

For the complete six-tool demo and roadmap, see [Tech Spend Command Center](https://github.com/cloudandcapital/tech-spend-command-center).

This repository does **not** mutate cloud resources, estimate savings, verify savings, or claim a root cause. An anomaly is an observed variance that requires review.

## What works today

- `watchdog ccac` consumes a `ccac/1.0.0` `tool_result` from FinOps Lite.
- It reads observed, additive, one-day currency metrics and preserves their declared dimensions.
- When reconciled daily service series are present, it analyzes those series and suppresses the overlapping provider-total series to prevent duplicate findings and downstream double-counting.
- It calculates a trailing median, median absolute deviation (MAD), robust score, observed excess cost, and percentage change.
- A finding must pass both statistical and financial materiality thresholds.
- Zero-to-positive spend is explicitly classified as `new_spend`.
- Series without enough history are reported as partial data quality, not silently treated as normal.
- Output is a versioned CCAC `tool_result` with source hash, run identity, evidence, metrics, and lifecycle-ready findings.
- `watchdog detect` still supports arbitrary local CSV files when column mappings are provided explicitly.

FinOps Lite 0.2 emits reconciled daily AWS service metrics and provider totals. Watchdog analyzes the service series and suppresses the overlapping provider parent, producing service-attributed findings without counting the same spike twice. It preserves other dimensions when an upstream producer supplies them; it never infers dimensions from period totals.

## Install

Python 3.10 or newer is required.

```bash
pipx install "git+https://github.com/cloudandcapital/finops-watchdog.git"
watchdog --help
```

For development from a clone:

```bash
python -m pip install -e ".[dev]"
```

## CCAC pipeline usage

Run the deterministic public example:

```bash
watchdog ccac --demo
```

**Illustrative sample billing data. No customer accounts, credentials, or production resources are connected.**

Consume a FinOps Lite result:

```bash
finops ccac --start 2026-07-01 --end 2026-07-31 --output finops-lite.json
watchdog ccac --input finops-lite.json --output watchdog.json
```

The acceptance suite validates this output against the shared CCAC reference schemas. Contributors may additionally run `ccac validate watchdog.json` after installing the separate CCAC reference package.

The real FinOps Lite command reads AWS Cost Explorer through read-only API calls. The public `--demo` path uses clearly illustrative local data and no cloud credentials.

Detection controls:

```text
--window-days INTEGER  Complete trailing days required; default 14
--threshold FLOAT      Minimum robust MAD score; default 3.0
--min-amount FLOAT     Minimum observed excess cost; default 10.0
--min-percent FLOAT    Minimum increase over trailing median; default 20.0
```

All gates must pass. When the trailing MAD is zero, a positive break from a constant baseline can still qualify if both financial thresholds pass. New spend is evaluated against an all-zero trailing window.

## Legacy CSV compatibility

The CSV command does not automatically map provider exports or FOCUS columns. Supply the exact timestamp, cost, and grouping columns:

```bash
watchdog detect \
  --input examples/sample_cost_data.csv \
  --time-column date \
  --value-column amount \
  --group-by SERVICE \
  --window 30d \
  --threshold 3.0 \
  --min-amount 10 \
  --output-format json
```

Legacy output formats are JSON, YAML, and CSV. The legacy algorithm remains a trailing arithmetic mean and population-standard-deviation detector for backward compatibility; it is not the CCAC median/MAD detector.

CSV users can select the same robust detector family used by the CCAC path:

```bash
watchdog detect \
  --input costs.csv \
  --time-column date \
  --value-column cost \
  --group-by service \
  --output-format json \
  --algorithm robust \
  --min-percent 20 \
  --fail-on-anomaly
```

Robust CSV mode requires consecutive, unique daily rows per group. Missing days fail closed instead of being interpreted as zero spend.

Legacy command exit codes are:

- `0`: analysis completed; by default this applies whether or not anomalies were found
- `1`: anomalies found when the opt-in `--fail-on-anomaly` flag is used
- `2`: invalid CLI usage
- `3`: input file error
- `4`: schema or data error
- `5`: unexpected internal error

Machine consumers should inspect the payload rather than treating a nonzero anomaly count as process failure.

## Trust and interpretation

- `impact` means observed cost above the trailing median on that day. It is not avoidable cost and not savings.
- Findings begin with lifecycle status `open`; later systems may mark them acknowledged, investigating, expected change, resolved, or dismissed.
- Watchdog does not produce opportunities, remediation commands, or root-cause assertions.
- Required financial values fail closed when absent, non-numeric, non-finite, negative, duplicated for a daily scope, or structurally incompatible.
- Illustrative output remains machine-labeled `mode: illustrative` and `data_classification: public_illustrative`.

## Development

```bash
uv run --extra dev pytest
```

The test suite covers legacy behavior, deterministic CCAC output, schema validation compatibility, malformed financial values, insufficient history, new spend, materiality suppression, dimension preservation, and injected-event detection quality.

## Pipeline compatibility

The verified connection in this phase is:

```text
FinOps Lite CCAC tool_result -> FinOps Watchdog CCAC tool_result -> Tech Spend Command Center trusted_report
```

The complete illustrative acceptance run passes independent CCAC validation. Cloud Cost Guard remains unchanged until its downstream adapter is reviewed separately.

| Component | Compatible version |
|---|---|
| FinOps Lite | `0.2.x` |
| FinOps Watchdog | `0.2.x` |
| CCAC | `ccac/1.0.0` |
| Tech Spend Command Center | `0.2.x` |

## License

MIT © 2025–2026 Diana Molski, Cloud & Capital
