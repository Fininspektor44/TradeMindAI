param(
    [string]$Start = "2022-01-01T00:00:00+00:00",
    [string]$End = "",
    [string]$Symbols = "",
    [switch]$RefreshHistory,
    [double]$CostBpsPerSide = 8.0
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found: $python" }

$argsList = @(
    "-m", "trademind.adaptive_trend_v135",
    "--start", $Start,
    "--cost-bps-per-side", $CostBpsPerSide.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)
if ($End) { $argsList += @("--end", $End) }
if ($Symbols) { $argsList += @("--symbols", $Symbols) }
if ($RefreshHistory) { $argsList += "--refresh-history" }

Write-Host "TradeMind v1.35 AdaptiveTrend reproduction" -ForegroundColor Cyan
Write-Host "Internet source: arXiv:2602.11708 (2026)" -ForegroundColor Cyan
Write-Host "H6 momentum -> ATR trailing -> previous-month optimisation -> Sharpe selection -> 70/30 allocation" -ForegroundColor Cyan
Write-Host "READ-ONLY. Public Bybit market data only. No orders/publication." -ForegroundColor Green
Write-Host "First run downloads H6 history and can take a few minutes." -ForegroundColor Yellow

& $python @argsList
if ($LASTEXITCODE -ne 0) { throw "v1.35 AdaptiveTrend reproduction failed" }

$statusPath = Join-Path $repo "data\adaptivetrend_v1_35\backtest\status.json"
if (Test-Path $statusPath) {
    $s = Get-Content $statusPath -Raw | ConvertFrom-Json
    Write-Host "`n===== v1.35 RESULT =====" -ForegroundColor Cyan
    [pscustomobject]@{
        Months = $s.months
        Trades = $s.trades
        WinRatePct = [math]::Round(100*[double]$s.win_rate,2)
        ProfitFactor = $s.profit_factor
        TotalReturnPct = [math]::Round(100*[double]$s.total_return,2)
        CAGRPct = [math]::Round(100*[double]$s.cagr,2)
        Sharpe = [math]::Round([double]$s.sharpe,3)
        MaxDrawdownPct = [math]::Round(100*[double]$s.max_drawdown,2)
        Calmar = [math]::Round([double]$s.calmar,3)
    } | Format-List
    Write-Host "`nLast monthly selections:" -ForegroundColor Cyan
    Import-Csv (Join-Path $repo "data\adaptivetrend_v1_35\backtest\monthly_selection.csv") |
        Select-Object -Last 8 month,long_count,short_count,trades,@{N="ReturnPct";E={[math]::Round(100*[double]$_.month_return,2)}},long_symbols,short_symbols |
        Format-Table -AutoSize
}
