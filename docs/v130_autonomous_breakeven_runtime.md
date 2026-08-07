# TradeMind v1.30 Autonomous BreakEven Runtime

## Purpose

v1.30 removes manual minute-by-minute work from the break-even research loop.
It does not implement break-even in a trading robot. It only orchestrates the
existing read-only analytics:

1. v1.28 reads the current MT5 position snapshot and updates shadow BE epochs.
2. v1.29 reads the current MT5 deal export and resolves completed baskets.
3. v1.30 writes one combined health/status file for later inspection.

## Live inputs

The default Windows runner reads only these existing exporter files:

- `MetaQuotes/Terminal/Common/Files/TradeMindAI/mt5_risk_positions_utc_<login>.csv`
- `MetaQuotes/Terminal/Common/Files/TradeMindAI/grid_deals_<login>.csv`

Exporter settings are not changed.

## Continuity

v1.30 deliberately reuses the existing output locations so previously collected
research is preserved:

- `data/breakeven_stat_monitor_v1/<login>/`
- `data/breakeven_counterfactual_v1/<login>/`

The new combined status is written to:

- `data/breakeven_runtime_v1/<login>/status.json`

## Scheduled task

`install_v130_breakeven_runtime_task.ps1` performs a full read-only runtime check
first. Only after the new scheduled task completes successfully does it disable
the older `TradeMindAI-v1.28-BreakEvenStats` task, avoiding duplicate minute runs.
The old task is disabled, not deleted.

Default task:

- name: `TradeMindAI-v1.30-BreakEvenRuntime`
- interval: one minute

## Safety boundary

The v1.30 runtime:

- does not call a broker API;
- does not send orders;
- does not modify SL or TP;
- does not change AOExtremum or MultiRSI source/settings;
- does not change exporter settings;
- does not modify source CSV files;
- only writes analytics under the TradeMind `data/` directory and Task Scheduler
  metadata for its own runtime task.

The result remains research evidence. It is not proof that a live break-even rule
improves profitability.
