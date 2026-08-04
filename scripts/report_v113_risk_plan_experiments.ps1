$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$statusPath = Join-Path $projectRoot "data\bybit_risk_plans_v1_13\status.json"
if (-not (Test-Path $statusPath)) {
    throw "Risk-plan status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$arms = @(
    "BASE_STRICT", "WIDE15_R15", "WIDE15_R20", "WIDE20_R15",
    "WIDE20_R20", "STRUCTURE_R15", "STRUCTURE_LIQ"
)

Write-Host "`n=== TRADEMIND v1.13 STOP / TARGET COMPARISON ===" -ForegroundColor Cyan
Write-Host "Started: $($status.started_at)"
Write-Host "Same STRICT_SELL entries, future signals only."
Write-Host "Net is a hypothetical estimate after fee, slippage and observed spread." -ForegroundColor Yellow
Write-Host "ForwardOnly: $($status.forward_only) | OrdersEnabled: $($status.orders_enabled) | LogicChanged: $($status.logic_changed)"

$rows = foreach ($arm in $arms) {
    $item = $status.arms.$arm
    [pscustomobject]@{
        Arm = $arm
        N = $item.completed
        Wins = $item.wins
        Losses = $item.losses
        Timeouts = $item.timeouts
        GrossR = [math]::Round([double]$item.gross_total_r, 4)
        GrossPF = [math]::Round([double]$item.gross_profit_factor, 3)
        CostR = [math]::Round([double]$item.estimated_cost_r, 4)
        NetR = [math]::Round([double]$item.net_total_r, 4)
        NetAvgR = [math]::Round([double]$item.net_average_r, 4)
        NetPF = [math]::Round([double]$item.net_profit_factor, 3)
        NetDD = [math]::Round([double]$item.net_max_drawdown_r, 3)
        SizeFactor = [math]::Round([double]$item.average_position_size_factor, 3)
    }
}
$rows | Sort-Object NetR -Descending | Format-Table -AutoSize
Write-Host "`nNo arm changes entries or sends orders. Do not select a winner on a tiny sample." -ForegroundColor Green
