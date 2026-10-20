# Supplied billing evidence (draft)

This adapter connects FinOps Lite's `billing_csv/1.0` CCAC output to the existing
Watchdog median/MAD detector. It does not implement a second anomaly detector.
The `billing` optional dependency pins the companion FinOps Lite draft and the
released CCAC validator. This integration is not a new production release.

```python
from finops_lite.ccac_json import loads_exact, dumps_exact
from finops_watchdog.billing import build_billing_result

result = build_billing_result(loads_exact(open('finops-lite.json').read()))
open('finops-watchdog.json', 'w').write(dumps_exact(result))
```

Before detection, the adapter validates the canonical producer, partial billed
Cloud boundary, source/evidence hashes, privacy classification, daily metric
inventory, periods, currency, and actual eligible values against normalized
supplied records. Eligible series require consecutive observed full UTC days
and nonnegative daily totals. Monthly intervals, negative days and gaps are
excluded with reasons; no day is spread or filled with zero. Fewer than the
existing history window is insufficient evidence, not proof of normal cost.

Detector thresholds, severity, materiality and findings remain owned by the
existing implementation. A finding reports unusual observed cost, not savings
or root cause. The upstream exact artifact hash binds the diagnostic to the
bill. No eligible observations yields a valid partial diagnostic with empty
findings, never substituted sample events. Existing AWS fixture pin checks,
legacy CLI and the credential-free demo remain unchanged.

Tests use invented bills, including an injected daily spike, short history,
monthly records, gaps, negative adjustments and adversarial lineage changes.
Guard runs these Python sources locally in a WebAssembly worker; no customer
bill is committed to GitHub or passed to a public workflow.
