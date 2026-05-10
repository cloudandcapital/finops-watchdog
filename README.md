# FinOps Watchdog

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Multi-cloud](https://img.shields.io/badge/cloud-AWS%20%7C%20Azure%20%7C%20GCP-orange)](https://github.com/cloudandcapital/finops-watchdog)

**Baseline-aware cost anomaly detection — surface economically meaningful spend spikes from any billing CSV.**

Part of the [Cloud & Capital](https://github.com/cloudandcapital) FinOps pipeline.  
Anomaly output feeds into [Cloud Cost Guard](https://github.com/cloudandcapital/cloud-cost-guard) — the unified FinOps dashboard.

---

**Features:**
- One command: `detect` — reads a cost CSV, returns anomalies
- Baseline-aware: rolling median + MAD (median absolute deviation) to minimize false positives
- Configurable threshold and minimum delta to suppress billing noise
- Machine-readable output: JSON, YAML, or CSV — pipe-friendly with exit codes
- Works with any cost CSV — AWS Cost Explorer, Azure exports, GCP billing, or FOCUS 2026
- Markdown report mode for async sharing

---

## Install

```bash
pip install "git+https://github.com/cloudandcapital/finops-watchdog.git"
# or
pipx install .
```

---

## Usage

```bash
# Detect anomalies with default threshold (15% above baseline)
finops-watchdog detect --input costs.csv

# Tighter threshold (flag anything 10%+ above baseline)
finops-watchdog detect --input costs.csv --threshold 0.10

# JSON output for downstream tools
finops-watchdog detect --input costs.csv --format json

# Generate a Markdown anomaly report
finops-watchdog detect --input costs.csv --report

# YAML output
finops-watchdog detect --input costs.csv --format yaml
```

**Exit codes:**
- `0` — no anomalies above threshold
- `1` — one or more anomalies detected
- `2` — input error (bad file, missing column)

---

## Input CSV Format

Watchdog expects a cost time-series CSV with at minimum:

| Column | Description |
|--------|-------------|
| `date` | ISO date (YYYY-MM-DD) |
| `service` or `group` | Service or cost grouping name |
| `cost` | Daily cost amount (numeric) |

FOCUS 2026 exports (`ChargePeriodStart`, `ServiceName`, `BilledCost`) are automatically mapped.

---

## How It Works

Watchdog uses a **rolling baseline** approach:
1. Build a baseline window (default: trailing 30 days before the detection window)
2. Compute per-service **median** and **MAD** (robust to billing outliers)
3. Flag any service where recent spend exceeds `baseline + threshold * baseline`
4. Suppress findings below `--min-delta` USD to ignore noise (e.g. $2 CloudWatch spike)

This approach deliberately avoids z-score methods that break under the non-normal distributions common in cloud billing.

---

## Part of the Cloud & Capital Pipeline

| Tool | Role |
|------|------|
| [FinOps Lite](https://github.com/cloudandcapital/finops-lite) | Cost pull + FOCUS 2026 export |
| **FinOps Watchdog** | Anomaly detection from cost CSVs |
| [Cloud Cost Guard](https://github.com/cloudandcapital/cloud-cost-guard) | Unified dashboard |
| [Recovery Economics](https://github.com/cloudandcapital/recovery-economics) | Resilience cost modeling |
| [AI Cost Lens](https://github.com/cloudandcapital/ai-cost-lens) | AI/LLM spend tracking |
| [SaaS Cost Analyzer](https://github.com/cloudandcapital/saas-cost-analyzer) | SaaS license governance |
| [Tech Spend Command Center](https://github.com/cloudandcapital/tech-spend-command-center) | Executive reporting |

---

## License

MIT © 2025 Diana Molski, Cloud & Capital
