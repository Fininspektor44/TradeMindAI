param(
    [ValidateRange(5, 100)]
    [int]$Recent = 30
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$signalsPath = Join-Path $projectRoot "data\bybit_shadow_v1_10\signals.csv"

if (-not (Test-Path $signalsPath)) {
    throw "Shadow signals file not found: $signalsPath"
}

function Convert-ToInvariantDouble {
    param([object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return 0.0 }
    $number = 0.0
    $ok = [double]::TryParse(
        [string]$Value,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$number
    )
    if (-not $ok) { return 0.0 }
    return $number
}

$all = @(Import-Csv $signalsPath)
$completed = @($all | Where-Object { [string]$_.completed -eq "1" })
$open = @($all | Where-Object { [string]$_.completed -ne "1" })
$wins = @($completed | Where-Object { [string]$_.outcome -eq "WIN" })
$losses = @($completed | Where-Object { [string]$_.outcome -eq "LOSS" })
$timeouts = @($completed | Where-Object { [string]$_.outcome -eq "TIMEOUT" })
$results = @($completed | ForEach-Object { Convert-ToInvariantDouble $_.result_r })
$positive = @($results | Where-Object { $_ -gt 0 })
$negative = @($results | Where-Object { $_ -lt 0 })
$totalR = if ($results.Count -gt 0) { ($results | Measure-Object -Sum).Sum } else { 0.0 }
$avgR = if ($results.Count -gt 0) { $totalR / $results.Count } else { 0.0 }
$winRate = if ($completed.Count -gt 0) { 100.0 * $wins.Count / $completed.Count } else { 0.0 }
$grossProfit = if ($positive.Count -gt 0) { ($positive | Measure-Object -Sum).Sum } else { 0.0 }
$grossLoss = if ($negative.Count -gt 0) { [math]::Abs(($negative | Measure-Object -Sum).Sum) } else { 0.0 }
$profitFactor = if ($grossLoss -gt 0) { $grossProfit / $grossLoss } elseif ($grossProfit -gt 0) { [double]::PositiveInfinity } else { 0.0 }

$equity = 0.0
$peak = 0.0
$maxDrawdownR = 0.0
foreach ($row in @($completed | Sort-Object { [int64]$_.start_ms })) {
    $equity += Convert-ToInvariantDouble $row.result_r
    if ($equity -gt $peak) { $peak = $equity }
    $drawdown = $peak - $equity
    if ($drawdown -gt $maxDrawdownR) { $maxDrawdownR = $drawdown }
}

Write-Host "`n=== TRADEMIND v1.10 BYBIT SHADOW FORWARD PERFORMANCE ===" -ForegroundColor Cyan
[pscustomobject]@{
    ForwardSignals = $all.Count
    Completed = $completed.Count
    Open = $open.Count
    Wins = $wins.Count
    Losses = $losses.Count
    Timeouts = $timeouts.Count
    WinRatePercent = [math]::Round($winRate, 2)
    TotalR = [math]::Round($totalR, 4)
    AverageR = [math]::Round($avgR, 4)
    ProfitFactor = if ([double]::IsPositiveInfinity($profitFactor)) { "INF" } else { [math]::Round($profitFactor, 3) }
    MaxDrawdownR = [math]::Round($maxDrawdownR, 4)
    OrdersEnabled = $false
} | Format-List

Write-Host "Results by symbol and direction:" -ForegroundColor Cyan
$groups = @(
    $completed |
        Group-Object symbol,action |
        ForEach-Object {
            $groupRows = @($_.Group)
            $groupResults = @($groupRows | ForEach-Object { Convert-ToInvariantDouble $_.result_r })
            $groupWins = @($groupRows | Where-Object { [string]$_.outcome -eq "WIN" }).Count
            $groupTotalR = if ($groupResults.Count -gt 0) { ($groupResults | Measure-Object -Sum).Sum } else { 0.0 }
            [pscustomobject]@{
                Symbol = [string]$groupRows[0].symbol
                Action = [string]$groupRows[0].action
                N = $groupRows.Count
                Wins = $groupWins
                Losses = @($groupRows | Where-Object { [string]$_.outcome -eq "LOSS" }).Count
                Timeouts = @($groupRows | Where-Object { [string]$_.outcome -eq "TIMEOUT" }).Count
                WinRatePercent = [math]::Round(100.0 * $groupWins / $groupRows.Count, 1)
                TotalR = [math]::Round($groupTotalR, 3)
                AverageR = [math]::Round($groupTotalR / $groupRows.Count, 3)
            }
        } |
        Sort-Object TotalR -Descending
)
if ($groups.Count -gt 0) {
    $groups | Format-Table -AutoSize
} else {
    Write-Host "No completed forward signals yet."
}

Write-Host "Recent shadow signals:" -ForegroundColor Cyan
$recentRows = @(
    $all |
        Sort-Object { [int64]$_.start_ms } -Descending |
        Select-Object -First $Recent `
            signal_time,symbol,action,quality_score,outcome,result_r,mfe_r,mae_r,completion_reason
)
if ($recentRows.Count -gt 0) {
    $recentRows | Format-Table -AutoSize
} else {
    Write-Host "No forward signals yet."
}

Write-Host "`nR is a normalized risk unit, not dollars. Fees, slippage and simultaneous-position risk are not included." -ForegroundColor Yellow
Write-Host "The report is research-only and never sends orders." -ForegroundColor Green
