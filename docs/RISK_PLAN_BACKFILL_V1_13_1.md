# TradeMind v1.13.1 Risk Plan Backfill

## Purpose

Run the seven v1.13 stop/target plans over historical eligible `STRICT_SELL` decisions without contaminating the live forward sample.

## Separation contract

- source decisions must have `eligible=1`, `action=SELL`;
- only decisions with `start_ms < forward started_at_ms` are included;
- the cutoff comes from `data/bybit_risk_plans_v1_13/experiment_meta.json`;
- output is written only to `data/bybit_risk_plans_backfill_v1_13_1`;
- `CONTROL`, `BUY_ONLY`, `STRICT_SELL` and forward v1.13 journals are never modified;
- backfill and forward metrics must never be merged into one sample.

## Arms

- `BASE_STRICT`
- `WIDE15_R15`
- `WIDE15_R20`
- `WIDE20_R15`
- `WIDE20_R20`
- `STRUCTURE_R15`
- `STRUCTURE_LIQ`

Each arm receives the same historical entry signal. Wider stops reduce the theoretical position-size factor so the assumed money risk stays equal.

## Outcome model

The replay uses stored Bybit M5 bars and the same paper outcome evaluator as the forward experiment. When one M5 candle touches both stop and target, the result is recorded conservatively as `STOP_FIRST_CONSERVATIVE`.

## Cost model

The report shows gross R and hypothetical net R after:

- fee per side;
- slippage per side;
- observed entry spread.

These are research estimates, not exchange invoices.

## Outputs

- `status.json`
- `comparison.csv`
- per-arm `decisions.csv`
- per-arm `signals.csv`
- `dashboard/index.html`

## Windows run

The backfill is intentionally one-shot and does not install a recurring Scheduled Task.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_v1131_risk_plan_backfill.ps1" -RunNow -OpenDashboard
```

## Safety

- `historical_only=True`
- `orders_enabled=False`
- `logic_changed=False`
- `forward_journals_modified=False`
- no trading API calls
