param(
    [ValidateRange(5, 100)]
    [int]$Recent = 20
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$root = Join-Path $projectRoot "data\bybit_shadow_v1_11"
$statusPath = Join-Path $root "status.json"
$arms = @("CONTROL", "BUY_ONLY", "STRICT_SELL")

if (-not (Test-Path $statusPath)) {
    throw "Experiment status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json

Write-Host "`n=== TRADEMIND v1.11 EQUAL-START FORWARD COMPARISON ===" -ForegroundColor Cyan
Write-Host "Started: $($status.started_at)"
Write-Host "ForwardOnly: $($status.forward_only) | OrdersEnabled: $($status.orders_enabled)"

$summaryRows = foreach ($arm in $arms) {
    $item = $status.arms.$arm
    [pscustomobject]@{
        Arm = $arm
        Signals = $item.signals
        Completed = $item.completed
        Open = $item.open
        Wins = $item.wins
        Losses = $item.losses
        Timeouts = $item.timeouts
        WinRatePct = [math]::Round(100.0 * [double]$item.win_rate, 2)
        TotalR = [math]::Round([double]$item.total_r, 4)
        AverageR = [math]::Round([double]$item.average_r, 4)
        ProfitFactor = [math]::Round([double]$item.profit_factor, 3)
        MaxDrawdownR = [math]::Round([double]$item.max_drawdown_r, 3)
    }
}
$summaryRows | Format-Table -AutoSize

foreach ($arm in $arms) {
    $signalsPath = Join-Path $root ("{0}\signals.csv" -f $arm.ToLower())
    Write-Host "`n$arm by symbol and direction:" -ForegroundColor Cyan
    if (-not (Test-Path $signalsPath)) {
        Write-Host "No journal yet."
        continue
    }
    $all = @(Import-Csv $signalsPath)
    $completed = @($all | Where-Object { [string]$_.completed -eq "1" })
    if ($completed.Count -eq 0) {
        Write-Host "No completed forward signals yet."
    } else {
        $groups = @(
            $completed |
                Group-Object symbol,action |
                ForEach-Object {
                    $rows = @($_.Group)
                    $values = @($rows | ForEach-Object { [double]::Parse([string]$_.result_r, [Globalization.CultureInfo]::InvariantCulture) })
                    $total = ($values | Measure-Object -Sum).Sum
                    $wins = @($values | Where-Object { $_ -gt 0 }).Count
                    [pscustomobject]@{
                        Symbol = [string]$rows[0].symbol
                        Action = [string]$rows[0].action
                        N = $rows.Count
                        Wins = $wins
                        Losses = @($values | Where-Object { $_ -lt 0 }).Count
                        WinRatePct = [math]::Round(100.0 * $wins / $rows.Count, 1)
                        TotalR = [math]::Round($total, 3)
                        AverageR = [math]::Round($total / $rows.Count, 3)
                    }
                } |
                Sort-Object TotalR -Descending
        )
        $groups | Format-Table -AutoSize
    }
    Write-Host "$arm recent signals:" -ForegroundColor DarkCyan
    $all |
        Sort-Object { [int64]$_.start_ms } -Descending |
        Select-Object -First $Recent signal_time,symbol,action,quality_score,outcome,result_r,mfe_r,mae_r,completion_reason |
        Format-Table -AutoSize
}

Write-Host "`nR is normalized risk, not dollars. Fees, slippage and portfolio correlation are not included." -ForegroundColor Yellow
Write-Host "The comparison is forward-only and never sends orders." -ForegroundColor Green
