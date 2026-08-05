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

$source = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI\grid_deals_$AccountLogin.csv"
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
    $legs = Join-Path $legsDir "$Name.csv"
    $report = Join-Path $reportsDir $Name
    New-Item -ItemType Directory -Force -Path $report | Out-Null

    $Data | Export-Csv $deals -NoTypeInformation -Encoding UTF8

    & ".\.venv\Scripts\python.exe" -m trademind.grid_deal_reconstruction --deals $deals --output $legs
    if ($LASTEXITCODE -ne 0) { throw "Reconstruction failed for $Name" }

    $args = @("-m", "trademind.grid_basket_audit", "--legs", $legs, "--output-dir", $report)
    if ($Open) { $args += "--open-dashboard" }
    & ".\.venv\Scripts\python.exe" @args
    if ($LASTEXITCODE -ne 0) { throw "Audit failed for $Name" }
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

Invoke-Audit -Name "combined_magic_$($Magic -join '_')" -Data $selected -Open:$OpenDashboard

foreach ($m in $Magic) {
    $group = @($selected | Where-Object { [string]$_.magic -eq $m })
    if ($group.Count -gt 0) {
        Invoke-Audit -Name "magic_$m" -Data $group
    }
}

Write-Host "`nReports:" -ForegroundColor Green
Write-Host $reportsDir
