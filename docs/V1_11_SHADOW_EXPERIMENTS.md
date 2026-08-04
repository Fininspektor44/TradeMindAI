# TradeMind v1.11 Shadow Experiments

Forward-only, read-only comparison that starts all experiment arms at the same UTC timestamp and consumes the same future Bybit M5 bars.

## Arms

- `CONTROL`: exact v1.10 scoring and candidate policy, copied into a fresh v1.11 journal for an equal-start comparison.
- `BUY_ONLY`: exact v1.10 candidates, but only BUY candidates may open paper signals.
- `STRICT_SELL`: a strict subset of v1.10 SELL candidates. It requires score >= 85 plus aligned H1/M15 price and delta, M15 and M5 book pressure, M5 delta impulse, acceptable spread, funding and basis.

The original `data/bybit_shadow_v1_10` journal is not changed or reset.

## Integrity rules

- All arms share one `started_at_ms` stored in `data/bybit_shadow_v1_11/experiment_meta.json`.
- Bars ending before the experiment start are ignored.
- Each arm owns a separate decisions and paper-signals journal.
- Existing v1.10 observations are never copied into v1.11.
- No order APIs are imported. `orders_enabled` remains false.
- The comparison is descriptive until each arm has enough completed forward observations across several trading days.
