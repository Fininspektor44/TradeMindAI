# TradeMind AI v1.4.3 FX Research Dashboard

## Purpose

v1.4.3 turns the read-only FX research CSV files into a standalone local HTML dashboard. It does not modify the frozen v1.3 Paper Gate, does not publish client signals and cannot open, modify or close MetaTrader orders.

The dashboard reads:

```text
data/fx_research_v1_4_2/observations.csv
data/fx_research_v1_4_2/latest.csv
```

and writes:

```text
data/fx_research_v1_4_2/dashboard/index.html
```

The HTML has no remote JavaScript, CSS, analytics or network dependency. It can be opened directly with `file:///` in a browser.

## What the dashboard shows

- total FX observations and completed H12 outcomes;
- counts of `VALIDATED`, `RESEARCH_CANDIDATE`, `UNSTABLE` and `INSUFFICIENT_SAMPLE` rows;
- an explicit commercial-readiness verdict;
- the strongest current evidence, clearly marked as provisional when the sample is insufficient;
- red flags ranked by negative expectancy, drawdown and loss streak;
- coverage cards for all seven FX majors;
- a searchable and filterable table by symbol, session, action, horizon, status and research label;
- win rate, PF in ATR, average net ATR, chronological halves, drawdown, loss streak, q-value and rejection reasons.

The internal evidence score is used only to order cards. It is not displayed as a probability, confidence score or expected return.

## Automatic refresh

The existing runner now performs three read-only steps:

```text
volume collect
-> FX research
-> FX dashboard render
```

Because the installed Windows Scheduled Task already points to `scripts/run_v142_fx_research.ps1`, no second scheduled task is required. After `git pull` and package reinstall, the dashboard refreshes every five minutes with the existing FX research cycle.

## First run and open

From the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\scripts\run_v142_fx_research.ps1" `
  -ServerUtcOffsetHours 3 `
  -OpenDashboard
```

For RoboForex summer server time the established research offset is `3`. Keep the offset unchanged inside one research period.

Expected output includes:

```text
TradeMind v1.4.3 FX research dashboard
Observations: ...
Validation states: ...
Validated: ...
Research candidates: ...
Dashboard: ...\dashboard\index.html
No orders were sent.
```

## Commercial discipline

A dashboard is not proof. Green cards are not a product licence. Promotion still requires:

1. a frozen rule set;
2. a later untouched out-of-sample boundary;
3. adequate distinct trading days and non-overlapping observations;
4. positive expectancy after spread;
5. stable chronological halves;
6. acceptable drawdown and loss streak;
7. multiple-testing control;
8. live paper delivery without execution defects.

Until those conditions are met, the dashboard must describe combinations as research observations or candidates, not as guaranteed signals.
