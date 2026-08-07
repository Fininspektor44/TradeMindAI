# TradeMind v1.31 BreakEven Decision Report

v1.31 turns the autonomous read-only BE research pipeline into a human-readable evidence report.

## Outputs

The existing autonomous runtime now refreshes:

- `data/breakeven_runtime_v1/<login>/report/summary.json`
- `data/breakeven_runtime_v1/<login>/report/index.html`

The report summarizes:

- completed baskets and shadow coverage;
- baskets where shadow BE actually would have mattered;
- losses avoided proxy count and money;
- winners cut proxy count and opportunity cost;
- net proxy effect;
- recent covered basket classifications.

## Evidence gate

The report remains `COLLECTING_EVIDENCE` until both conditions are met:

- at least 30 baskets where the observed shadow BE revisit would have affected the outcome;
- at least 80% coverage of completed baskets.

After that it becomes `READY_FOR_HUMAN_REVIEW`. This is not an automated recommendation to enable or disable BE.

## Safety boundary

v1.31 is analytics only:

- no broker API;
- no order send/close/modify;
- no robot setting changes;
- no exporter setting changes;
- source CSV files remain unchanged;
- exact hypothetical commissions, slippage and intraminute touches are not fabricated.
