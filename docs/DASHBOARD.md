# TradeMind Validation Dashboard

TradeMind v1.1 generates a standalone local HTML dashboard. It does not require a web server,
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

## Validation policy

The dashboard does not promote cross-symbol portfolio aggregates into candidates. A candidate must
belong to one specific instrument and one specific horizon.

A group becomes a research candidate only when:

- it has at least 30 evaluated non-overlapping trades;
- the early chronological half has positive average net ATR and PF_ATR above 1.0;
- the late chronological half has positive average net ATR and PF_ATR above 1.0;
- the overall result after spread is positive.

A group becomes validated only when:

- all candidate conditions remain true;
- it has at least 300 evaluated non-overlapping trades;
- the lower bound of the approximate 95% confidence interval for mean net ATR is above zero.

Validated does not mean production-ready. Market-regime, execution and out-of-sample checks are
still required before signal weights may change.

## Risk and stability fields

The full table shows:

- early-half and late-half average net ATR;
- maximum cumulative drawdown in ATR units;
- maximum consecutive losing-trade streak;
- approximate 95% confidence interval for mean net ATR;
- `UNSTABLE` when one half or the total result is not positive;
- `PORTFOLIO_ONLY` for cross-symbol informational rows.

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

### Pattern sections

Confirmed patterns contain only `VALIDATED` per-symbol groups. Stable research candidates contain
only `RESEARCH_CANDIDATE` per-symbol groups. Portfolio aggregates remain visible in the full table
but cannot appear in either promotion section.

### Full validation table

The table contains portfolio-normalized and per-symbol rows for:

- internal and swing BOS;
- internal and swing CHoCH;
- BSL and SSL sweeps;
- bullish and bearish FVG;
- volume and spread cuts;
- aligned and conflicting internal/swing structure.

Filters narrow the table by instrument, horizon and validation status.

## Direct CLI use

```powershell
.\.venv\Scripts\trademind-dashboard.exe `
  --journal .\data\journal_ecn\signals.csv `
  --output .\data\dashboard\index.html `
  --candidate-min 30 `
  --min-sample 300
```

Run the text validator directly:

```powershell
.\.venv\Scripts\trademind-validate.exe `
  --journal .\data\journal_ecn\signals.csv `
  --candidate-min 30 `
  --min-sample 300
```

The dashboard and validator remain read-only. They never send an order to MetaTrader 5 and do not
change the BUY, SELL or WAIT score.
