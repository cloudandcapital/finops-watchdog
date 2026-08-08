# FinOps Watchdog

FinOps Watchdog detects financially material increases in daily cloud cost. Version 0.5 preserves the released CCAC 1.0 behavior and adds an explicit CCAC 1.1 anomaly-compatibility path. The original explicitly mapped CSV command remains available for compatibility.

For the complete six-tool demo and roadmap, see [Tech Spend Command Center](https://github.com/cloudandcapital/tech-spend-command-center).

This repository does **not** mutate cloud resources, estimate savings, verify savings, or claim a root cause. An anomaly is an observed variance that requires review.

## What works today

- `watchdog ccac` defaults to the byte-compatible `ccac/1.0.0` path and accepts explicit `--contract-version 1.0.0` or `--contract-version 1.1.0` selection.
- The 1.1 path consumes and emits `ccac/1.1.0` without copying FinOps Lite's canonical Cloud scope into Watchdog output.
- It reads observed, additive, one-day currency metrics and preserves their declared dimensions.
- When reconciled daily service series are present, it analyzes those series and suppresses the overlapping provider-total series to prevent duplicate findings and downstream double-counting.
- It calculates a trailing median, median absolute deviation (MAD), robust score, observed excess cost, and percentage change.
- A finding must pass both statistical and financial materiality thresholds.
- Zero-to-positive spend is explicitly classified as `new_spend`.
- Series without enough history are reported as partial data quality, not silently treated as normal.
- Output is a versioned CCAC `tool_result` with source hash, run identity, evidence, metrics, and lifecycle-ready findings.
- `watchdog detect` still supports arbitrary local CSV files when column mappings are provided explicitly.

Compatible FinOps Lite `0.3.0` and `0.4.0` inputs emit reconciled daily AWS service metrics and provider totals. CCAC 1.1 requires FinOps Lite `0.4.0`. Watchdog analyzes the service series and suppresses the overlapping provider parent, producing service-attributed findings without counting the same spike twice. It preserves other dimensions when an upstream producer supplies them; it never infers dimensions from period totals.

The public demo is credential-free and uses entirely illustrative data.

## Install the released CLI

Python 3.10 or newer is required.

```bash
pipx install "git+https://github.com/cloudandcapital/finops-watchdog.git@v0.4.0"
watchdog --help
```

For development from a clone:

```bash
python -m pip install -e ".[dev]"
```

## CCAC pipeline usage

Run the deterministic public example:

```bash
watchdog ccac --demo --output watchdog-result.json
watchdog ccac --demo --contract-version 1.1.0 --output watchdog-result-1.1.json
```

The command writes `watchdog-result.json`; rerunning with the same path
replaces that explicitly named local file.

**Illustrative sample billing data. No customer accounts, credentials, or production resources are connected.**

Consume a FinOps Lite result:

```bash
finops ccac --start 2026-07-01 --end 2026-07-21 --contract-version 1.1.0 --output finops-lite.json
watchdog ccac --input finops-lite.json --contract-version 1.1.0 --output watchdog.json
```

Input and output contracts must match exactly; Watchdog never infers, upgrades, downgrades, or translates a contract. The acceptance suite validates output against the released CCAC 0.2.0 reference package. The packaged 1.1 demonstration is the byte-exact deterministic FinOps Lite artifact generated from commit `d72649ec07aa57c60a7ea3f8ff2890b8d95c4b93`, SHA-256 `ae40d79949a0f6abccaf0f810e602eae8649b02c9a1379af405af2a1fc97b3ac`.

Watchdog 1.1 emits anomaly diagnostics and findings only. It does not own or repeat `metric.tech-spend.scope.cloud`, emit another canonical technology-spend scope, or advertise an all-in technology-spend total. Real and custom inputs remain read-only, partial in organizational coverage, and ineligible for an all-in total.

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

The CSV command does not automatically map provider exports or FOCUS columns. From a cloned checkout, the repository's inspectable [cost time-series fixture](examples/cost-timeseries-sample.csv) can be run with its exact timestamp, cost, and grouping columns:

```bash
watchdog detect \
  --input examples/cost-timeseries-sample.csv \
  --time-column date \
  --value-column amount \
  --group-by SERVICE \
  --window 30d \
  --threshold 3.0 \
  --min-amount 10 \
  --output-format json
```

Installed users can instead pass their own local CSV to `--input` and must supply the column names that actually exist in that file through `--time-column`, `--value-column`, and `--group-by`. No provider-export or FOCUS column mapping is inferred automatically.

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
| FinOps Lite | `0.3.0` for CCAC 1.0; `0.4.0` for CCAC 1.0 and 1.1 |
| FinOps Watchdog | `0.5.x` |
| CCAC | `ccac/1.0.0`, `ccac/1.1.0` (CCAC package 0.2.0) |
| Tech Spend Command Center | `0.2.x` |

## License

MIT © 2025–2026 Diana Molski, Cloud & Capital
