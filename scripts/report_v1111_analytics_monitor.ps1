param(
    [ValidateRange(1, 50)]
    [int]$Top = 10
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$root = Join-Path $projectRoot "data\bybit_shadow_monitor_v1_11_1"
$statusPath = Join-Path $root "status.json"
$breakdownsPath = Join-Path $root "breakdowns.csv"
$arms = @("CONTROL", "BUY_ONLY", "STRICT_SELL")
if (-not (Test-Path $statusPath)) {
    throw "Analytics status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json

Write-Host "`n=== TRADEMIND v1.11.1 NET FORWARD ANALYTICS ===" -ForegroundColor Cyan
Write-Host "Experiment started: $($status.experiment_started_at)"
Write-Host "Cost model is hypothetical, not an exchange invoice." -ForegroundColor Yellow
Write-Host "Fee: $($status.cost_model.fee_bps_per_side) bps/side | Slippage: $($status.cost_model.slippage_bps_per_side) bps/side | Entry spread included: $($status.cost_model.observed_entry_spread_included)"
Write-Host "ForwardOnly: $($status.forward_only) | OrdersEnabled: $($status.orders_enabled) | LogicChanged: $($status.logic_changed)"

$summary = foreach ($arm in $arms) {
    $item = $status.arms.$arm
    [pscustomobject]@{
        Arm = $arm
        Completed = $item.completed
        GrossR = [math]::Round([double]$item.gross_total_r, 4)
        CostR = [math]::Round([double]$item.estimated_cost_r, 4)
        NetR = [math]::Round([double]$item.net_total_r, 4)
        NetAvgR = [math]::Round([double]$item.net_average_r, 4)
        NetPF = [math]::Round([double]$item.net_profit_factor, 3)
        NetMaxDD = [math]::Round([double]$item.net_max_drawdown_r, 3)
        PeakConcurrent = $item.peak_concurrent
        PeakSameDirection = $item.peak_same_direction
        LargestCluster = $item.largest_entry_cluster
        NextN = if ($null -eq $item.next_milestone) { "DONE" } else { $item.next_milestone }
        Remaining = $item.milestones.remaining
        Alert = [string]$item.alert
    }
}
$summary | Format-Table -AutoSize

if (Test-Path $breakdownsPath) {
    $all = @(Import-Csv $breakdownsPath)
    foreach ($arm in $arms) {
        Write-Host "`n$arm top symbols after estimated costs:" -ForegroundColor Cyan
        $all |
            Where-Object { $_.arm -eq $arm -and $_.dimension -eq "symbol" } |
            Sort-Object { [double]::Parse([string]$_.net_total_r, [Globalization.CultureInfo]::InvariantCulture) } -Descending |
            Select-Object -First $Top @{N="Symbol";E={$_.key}},completed,@{N="NetR";E={[math]::Round([double]::Parse([string]$_.net_total_r,[Globalization.CultureInfo]::InvariantCulture),3)}},@{N="NetAvgR";E={[math]::Round([double]::Parse([string]$_.net_average_r,[Globalization.CultureInfo]::InvariantCulture),3)}},@{N="NetPF";E={[math]::Round([double]::Parse([string]$_.net_profit_factor,[Globalization.CultureInfo]::InvariantCulture),3)}} |
            Format-Table -AutoSize

        Write-Host "$arm UTC hours after estimated costs:" -ForegroundColor DarkCyan
        $all |
            Where-Object { $_.arm -eq $arm -and $_.dimension -eq "utc_hour" } |
            Sort-Object { [int]$_.key } |
            Select-Object @{N="UTC";E={$_.key}},completed,@{N="NetR";E={[math]::Round([double]::Parse([string]$_.net_total_r,[Globalization.CultureInfo]::InvariantCulture),3)}},@{N="NetAvgR";E={[math]::Round([double]::Parse([string]$_.net_average_r,[Globalization.CultureInfo]::InvariantCulture),3)}} |
            Format-Table -AutoSize
    }
}

Write-Host "`nAlerts are descriptive only. Rules are not changed automatically." -ForegroundColor Yellow
Write-Host "The monitor is read-only and never sends orders." -ForegroundColor Green
