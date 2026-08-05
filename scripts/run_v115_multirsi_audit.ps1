param(
    [Parameter(Mandatory=$false)]
    [long]$AccountLogin = 37353316,

    [Parameter(Mandatory=$false)]
    [string[]]$Magic = @("8035", "8"),

    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$common = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI"
$source = Join-Path $common "grid_deals_$AccountLogin.csv"
$positions = Join-Path $common "grid_positions_$AccountLogin.csv"
$account = Join-Path $common "grid_account_$AccountLogin.csv"

if (!(Test-Path $source)) {
    throw "Source file not found: $source"
}

$rows = @(Import-Csv $source)
if ($rows.Count -eq 0) {
    throw "Source file is empty: $source"
}

$selected = @($rows | Where-Object { $Magic -contains [string]$_.magic })
if ($selected.Count -eq 0) {
    throw "No rows found for Magic: $($Magic -join ', ')"
}

$unexpected = @($rows | Where-Object { $Magic -notcontains [string]$_.magic })

$workRoot = Join-Path $repo "data\multirsi_v1_15\account_$AccountLogin"
$dealsDir = Join-Path $workRoot "deals"
$legsDir = Join-Path $workRoot "legs"
$reportsDir = Join-Path $workRoot "reports"
New-Item -ItemType Directory -Force -Path $dealsDir, $legsDir, $reportsDir | Out-Null

function Invoke-Audit {
    param(
        [string]$Name,
        [object[]]$Data,
        [switch]$Open
    )

    $deals = Join-Path $dealsDir "$Name.csv"
    $rawLegs = Join-Path $legsDir "$Name.raw.csv"
    $measuredLegs = Join-Path $legsDir "$Name.csv"
    $report = Join-Path $reportsDir $Name
    $snapshotReport = Join-Path $report "snapshots"
    New-Item -ItemType Directory -Force -Path $report, $snapshotReport | Out-Null

    $Data | Export-Csv $deals -NoTypeInformation -Encoding UTF8

    & ".\.venv\Scripts\python.exe" -m trademind.grid_deal_reconstruction `
        --deals $deals `
        --output $rawLegs
    if ($LASTEXITCODE -ne 0) {
        throw "Reconstruction failed for $Name"
    }

    if (Test-Path $positions) {
        $snapshotArgs = @(
            "-m", "trademind.grid_snapshot_drawdown",
            "--legs", $rawLegs,
            "--positions", $positions,
            "--output", $measuredLegs,
            "--summary-dir", $snapshotReport
        )
        if (Test-Path $account) {
            $snapshotArgs += @("--account-snapshots", $account)
        }
        & ".\.venv\Scripts\python.exe" @snapshotArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Snapshot drawdown enrichment failed for $Name"
        }
    }
    else {
        Copy-Item $rawLegs $measuredLegs -Force
        Write-Host "[WARN] Position snapshots not found; drawdown remains unmeasured: $positions" `
            -ForegroundColor Yellow
    }

    $auditArgs = @(
        "-m", "trademind.grid_basket_audit",
        "--legs", $measuredLegs,
        "--output-dir", $report
    )
    if ($Open) {
        $auditArgs += "--open-dashboard"
    }
    & ".\.venv\Scripts\python.exe" @auditArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Audit failed for $Name"
    }
}

$ordered = @($selected | Sort-Object {[long]$_.time_msc})
$first = [datetimeoffset]::FromUnixTimeMilliseconds([long]$ordered[0].time_msc).ToLocalTime()
$last = [datetimeoffset]::FromUnixTimeMilliseconds([long]$ordered[-1].time_msc).ToLocalTime()

Write-Host "`n=== MULTIRSI FAMILY AUDIT ===" -ForegroundColor Cyan
Write-Host "Account: $AccountLogin"
Write-Host "Magic: $($Magic -join ', ')"
Write-Host "Deals: $($selected.Count)"
Write-Host "First deal: $($first.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Last deal:  $($last.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Unexpected rows excluded: $($unexpected.Count)"
Write-Host "Position snapshots: $(if (Test-Path $positions) { $positions } else { 'MISSING' })"
Write-Host "Account snapshots:  $(if (Test-Path $account) { $account } else { 'MISSING' })"

Invoke-Audit -Name "combined_magic_$($Magic -join '_')" -Data $selected -Open:$OpenDashboard

foreach ($m in $Magic) {
    $group = @($selected | Where-Object { [string]$_.magic -eq $m })
    if ($group.Count -gt 0) {
        Invoke-Audit -Name "magic_$m" -Data $group
    }
}

Write-Host "`nReports:" -ForegroundColor Green
Write-Host $reportsDir
Write-Host "The basket dashboard now uses measured online floating drawdown where snapshots exist." `
    -ForegroundColor Green
Write-Host "Historical floating drawdown before the collector started remains unavailable." `
    -ForegroundColor Yellow
