# TradeMind Candidate Watcher

TradeMind v1.2 records how every per-symbol research pattern changes over time. It remains read-only:
it does not change signal weights, create orders or approve live trading.

## What is tracked

For every configured instrument, feature and horizon, the watcher stores:

- observation and non-overlapping trade counts;
- validation status;
- win rate, `PF_ATR` and average net ATR after spread;
- early-half and late-half average result;
- maximum drawdown and maximum loss streak;
- approximate 95% interval for mean net ATR;
- validation reasons.

Cross-symbol `ALL` aggregates are excluded. Only individual instruments can enter candidate history.

## Files

The default directory is:

```text
data\candidate_history
```

It contains:

- `history.csv`: append-only snapshots, written only when a pattern changes;
- `latest.csv`: atomic current-state snapshot for every pattern;
- `events.csv`: important status transitions only.

Repeated runs with unchanged data do not duplicate history rows.

## Events

The watcher can emit:

- `CANDIDATE_REACHED`: a pattern became a stable research candidate;
- `CANDIDATE_THRESHOLD_REJECTED`: it reached 30 trades but failed stability rules;
- `CANDIDATE_LOST`: a former candidate became unstable or otherwise failed;
- `VALIDATED`: the 300-trade and positive-CI rule was passed;
- `VALIDATION_LOST`: a previously validated pattern stopped passing;
- `BECAME_UNSTABLE` or `STATUS_CHANGED`: other important reclassifications.

The first run creates a baseline and does not manufacture transition events from missing history.

## Manual run

```powershell
.\.venv\Scripts\trademind-candidate-watch.exe `
  --journal .\data\journal_ecn\signals.csv `
  --history-dir .\data\candidate_history `
  --candidate-min 30 `
  --min-sample 300
```

The dashboard-generation script runs the watcher automatically unless `-SkipCandidateWatch` is
specified. The daily research task archives the watcher summary inside the dated text report.

## Interpretation

A transition to `RESEARCH_CANDIDATE` is a research milestone, not permission to trade. A candidate
must still accumulate the full validation sample, survive regime checks and pass execution-risk
controls before any future paper-trading or live-trading decision.
