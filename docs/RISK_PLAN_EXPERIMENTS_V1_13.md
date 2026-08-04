# TradeMind v1.13 Risk Plan Experiments

## Purpose

Compare stop and target plans on the same future `STRICT_SELL` entries without changing the entry policy or sending orders.

## Equal-start arms

- `BASE_STRICT`: existing stop, target and 12-bar horizon.
- `WIDE15_R15`: stop at least 1.5 M5 ATR, target 1.5R, 18 M5 bars.
- `WIDE15_R20`: stop at least 1.5 M5 ATR, target 2R, 18 M5 bars.
- `WIDE20_R15`: stop at least 2 M5 ATR, target 1.5R, 24 M5 bars.
- `WIDE20_R20`: stop at least 2 M5 ATR, target 2R, 24 M5 bars.
- `STRUCTURE_R15`: stop above the recent M5 swing plus 0.2 ATR, with a minimum 1.5 ATR and a cost floor, target 1.5R.
- `STRUCTURE_LIQ`: the same hybrid stop, target at downside liquidity while preserving at least 1.5R.

## Same money risk

Every arm records a theoretical `position_size_factor` equal to original risk distance divided by the new risk distance. A wider stop therefore implies a smaller theoretical position. The experiment does not calculate or submit real quantity.

## Cost model

Gross and hypothetical net results are stored together. Net subtracts:

- 5.5 bps fee per side by default;
- 1.0 bps slippage per side by default;
- observed entry spread from the original decision.

The structure hybrid enforces a minimum stop distance intended to keep estimated round-trip cost at or below 0.20R.

## Forward-only contract

- a new shared start timestamp is created on first run;
- only later eligible `STRICT_SELL` decisions are mirrored;
- old v1.11 journals are not copied into the experiment;
- all seven arms receive the same source decisions;
- `orders_enabled=False` and `logic_changed=False` are mandatory;
- the source `CONTROL`, `BUY_ONLY` and `STRICT_SELL` tasks are not modified.

## Outputs

- `data/bybit_risk_plans_v1_13/status.json`
- one `decisions.csv`, `signals.csv` and `status.json` per arm;
- `data/bybit_risk_plans_v1_13/dashboard/index.html`;
- scheduled task `TradeMindAI-v1.13-RiskPlanExperiments` every five minutes.
