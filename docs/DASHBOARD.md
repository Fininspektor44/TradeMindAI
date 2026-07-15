# TradeMind Research Dashboard

TradeMind v1.0 generates a standalone local HTML dashboard. It does not require a web server,
database or JavaScript package manager. The file can be opened directly in a browser on the SER8.

## Generate and open on Windows

```powershell
.\scripts\generate_dashboard.ps1 -Open
```

The default output is:

```text
data\dashboard\index.html
```

The daily research task also regenerates this file after creating the dated text report.

## Dashboard sections

### System health

The top status combines the health of all configured MT5 CSV files and the signal journal:

- `OK`: all required data is present and fresh;
- `WARN`: data exists but contains a non-critical issue such as zero spread or weekend staleness;
- `ERROR`: a required file is missing, invalid, duplicated or stale during an active session.

### Instrument cards

Every instrument card shows:

- schema `1.1` observation count;
- evaluated non-overlapping trade count for horizons 3, 6 and 12;
- CSV row count and age;
- latest spread and tick volume.

### Confirmed patterns

A pattern appears in the confirmed section only when all conditions are true:

- the group has reached `--min-sample`, default `300`, evaluated non-overlapping trades;
- ATR-normalized profit factor is above `1.0`;
- average net result after spread is positive.

Passing these gates does not make the pattern production-ready. It still requires stability checks
across time periods and market regimes.

### Research candidates

Candidates are early positive groups with at least 10 evaluated trades but fewer than the minimum
research sample. They are displayed only as investigation targets and must not change signal weights.

### Full research table

The table contains portfolio-normalized and per-symbol rows for:

- internal and swing BOS;
- internal and swing CHoCH;
- BSL and SSL sweeps;
- bullish and bearish FVG;
- volume and spread cuts;
- aligned and conflicting internal/swing structure.

Filters allow the user to narrow the table by instrument, horizon and sample status.

## Direct CLI use

```powershell
.\.venv\Scripts\trademind-dashboard.exe `
  --journal .\data\journal_ecn\signals.csv `
  --output .\data\dashboard\index.html `
  --min-sample 300
```

The dashboard remains read-only. It never sends an order to MetaTrader 5 and does not change the
BUY, SELL or WAIT score.
