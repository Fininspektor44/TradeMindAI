# TradeMind AI v1.3: action-aware validation and paper gate

TradeMind v1.3 keeps the live system read-only. It adds the trade direction to every research key and starts a frozen out-of-sample paper journal.

## What changed

A v1.3 validation state is identified by:

```text
symbol + feature + action + horizon
```

`BUY` and `SELL` are never mixed. Structure breaks are also split into directional labels such as:

```text
BULLISH_INTERNAL_BOS
BEARISH_INTERNAL_BOS
BULLISH_SWING_CHOCH
BEARISH_SWING_CHOCH
```

The validator reports non-overlapping trade count, distinct trading-day count, win rate, PF_ATR, average net ATR after spread, chronological halves, late-to-early edge retention, maximum drawdown, loss streak, CI95, p-value and Benjamini-Hochberg q-value.

A pattern cannot become `VALIDATED` unless the sample, positive CI95 and FDR checks pass.

## Frozen training boundary

The committed paper config uses:

```text
2026-07-31T00:00:00+00:00
```

Rows before this timestamp are training evidence. Rows at or after it are out-of-sample observations. Changing the boundary after results arrive invalidates the test.

## Locked rules

The config lives at:

```text
config/paper_gate_v1.3.json
```

`PRIMARY_OOS` is the strict rule that passed the configured multiple-testing threshold at the frozen boundary. `SHADOW_OOS` rules are recorded for comparison but are not production approval.

## Commands

Run action-aware validation:

```powershell
.\.venv\Scripts\trademind-action-validate.exe `
  --journal .\data\journal_ecn\signals.csv `
  --output .\data\action_validation\latest.csv
```

Run the paper gate:

```powershell
.\.venv\Scripts\trademind-paper-gate.exe `
  --journal .\data\journal_ecn\signals.csv `
  --config .\config\paper_gate_v1.3.json `
  --output .\data\paper_signals\signals.csv `
  --status-output .\data\paper_signals\gate_status.csv
```

Run both:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_v13_research.ps1
```

Install the five-minute Windows paper task:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_v13_paper_task.ps1 -RunNow
```

## Safety rule

The v1.3 commands only read the existing signal journal and write research CSV files. They contain no MetaTrader order API and cannot open, modify or close trades.
