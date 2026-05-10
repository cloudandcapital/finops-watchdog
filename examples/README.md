# Examples

`cost-timeseries-sample.csv` contains 31 days of daily cost data for two services. AmazonEC2 has a spike on Jan 31 (≈2.7× the baseline average). AmazonS3 is stable throughout.

## Detect anomalies

```bash
finops-watchdog detect \
  --input examples/cost-timeseries-sample.csv \
  --time-column date \
  --value-column amount \
  --group-by SERVICE \
  --window 30d \
  --threshold 2.5 \
  --output-format json
```

Expected output (abbreviated):

```json
{
  "schema_version": "1.0",
  "summary": {
    "total_anomalies": 1,
    "groups_impacted": 1,
    "max_delta_pct": 173.9
  },
  "anomalies": [
    {
      "timestamp": "2026-01-31T00:00:00Z",
      "group": "AmazonEC2",
      "baseline": 141.82,
      "current": 388.40,
      "delta": 246.58,
      "delta_pct": 173.9,
      "severity": "critical",
      "anomaly_type": "spend_above_threshold"
    }
  ]
}
```

## Add a markdown summary

```bash
finops-watchdog detect \
  --input examples/cost-timeseries-sample.csv \
  --time-column date \
  --value-column amount \
  --group-by SERVICE \
  --window 30d \
  --threshold 2.5 \
  --output-format json \
  --report anomaly-report.md
```

`anomaly-report.md` will contain a formatted markdown table suitable for pasting into a Slack message, a PR description, or a weekly cost review.
