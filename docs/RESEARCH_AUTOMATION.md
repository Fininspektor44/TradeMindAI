# TradeMind Research Automation

TradeMind v1.1 keeps read-only operational control around the ECN research pipeline. It does not
change BUY, SELL or WAIT decisions and does not send orders.

## Health checks

`trademind-health` verifies:

- every configured MT5 CSV file exists and contains the required candle columns;
- the latest candle is recent enough;
- the latest spread and tick volume are positive;
- recent unexplained candle gaps are counted;
- the research journal exists and contains schema `1.1` observations for every symbol;
- signal IDs are unique;
- the journal is receiving fresh observations.

Stale weekday data, missing files, invalid CSV data and duplicate signal IDs are `ERROR` items.
Stale weekend data and non-positive spread or volume are `WARN` items.

Example:

```powershell
.\.venv\Scripts\trademind-health.exe `
  --data-dir "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_ECN" `
  --journal .\data\journal_ecn\signals.csv `
  --timeframe M5 `
  --max-age-minutes 30
```

## Daily report

`scripts/generate_daily_research_report.ps1` creates a dated UTF-8 text report containing:

1. market-data and journal health;
2. non-overlapping SMC research for horizons 3, 6 and 12 candles;
3. portfolio-normalized informational sections;
4. complete per-symbol sections;
5. per-symbol temporal validation with early and late history halves;
6. maximum drawdown, loss streak and approximate CI95 diagnostics.

Reports are stored under:

```text
data\research_reports\YYYY-MM-DD\research_YYYY-MM-DD_HHMMSS.txt
```

The newest report is also copied to:

```text
data\research_reports\latest.txt
```

The HTML validation dashboard is regenerated at:

```text
data\dashboard\index.html
```

Run one report manually:

```powershell
.\scripts\generate_daily_research_report.ps1 `
  -CandidateMinimum 30 `
  -MinimumSample 300
```

## Scheduled task

Install a hidden daily task at 23:55 local Windows time:

```powershell
.\scripts\install_daily_research_task.ps1 -DailyTime "23:55" -RunNow
```

The time can be changed later by rerunning the installer with another `-DailyTime` value. The task
uses the current Windows user and runs only when that user is logged on. `-RunNow` is intended for
immediate validation after installation.

## Interpretation rule

A generated report is operational evidence, not permission to trade. `PORTFOLIO_ONLY` rows are
informational. `INSUFFICIENT_SAMPLE` and `UNSTABLE` rows must not alter signal weights. A
`RESEARCH_CANDIDATE` must still reach the full research sample and a positive lower CI95 bound before
becoming `VALIDATED`. Health `ERROR` items must be resolved before statistics are trusted.
