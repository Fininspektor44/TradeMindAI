param(
    [Parameter(Mandatory=$false)]
    [long]$AOAccount = 37365712,

    [Parameter(Mandatory=$false)]
    [long]$MultiAccount = 37353316,

    [Parameter(Mandatory=$false)]
    [string[]]$MultiMagic = @("8035", "8"),

    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (!(Test-Path $python)) {
    throw "Python environment not found: $python"
}

$common = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI"
$root = Join-Path $repo "data\control_center_v1_15"
$accountsRoot = Join-Path $root "accounts"
$reportsRoot = Join-Path $root "reports"
New-Item -ItemType Directory -Force -Path $accountsRoot, $reportsRoot | Out-Null

function Invoke-AccountAudit {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Name,

        [Parameter(Mandatory=$true)]
        [long]$AccountLogin,

        [Parameter(Mandatory=$true)]
        [string]$WorkDir,

        [Parameter(Mandatory=$true)]
        [string]$ReportDir,

        [string[]]$Magic = @()
    )

    $sourceDeals = Join-Path $common "grid_deals_$AccountLogin.csv"
    $sourcePositions = Join-Path $common "grid_positions_$AccountLogin.csv"
    $sourceAccount = Join-Path $common "grid_account_$AccountLogin.csv"

    foreach ($required in @($sourceDeals, $sourcePositions, $sourceAccount)) {
        if (!(Test-Path $required)) {
            throw "$Name source file not found: $required"
        }
    }

    New-Item -ItemType Directory -Force -Path $WorkDir, $ReportDir | Out-Null
    $deals = Join-Path $WorkDir "deals.csv"
    $rawLegs = Join-Path $WorkDir "basket_legs.raw.csv"
    $legs = Join-Path $WorkDir "basket_legs.csv"
    $snapshotReport = Join-Path $ReportDir "snapshots"
    New-Item -ItemType Directory -Force -Path $snapshotReport | Out-Null

    $rows = @(Import-Csv $sourceDeals)
    if ($rows.Count -eq 0) {
        throw "$Name deal export is empty: $sourceDeals"
    }

    if ($Magic.Count -gt 0) {
        $selected = @($rows | Where-Object { $Magic -contains [string]$_.magic })
    }
    else {
        $selected = $rows
    }
    if ($selected.Count -eq 0) {
        throw "$Name has no deals after Magic filtering: $($Magic -join ', ')"
    }

    $selected | Sort-Object {[long]$_.time_msc} |
        Export-Csv $deals -NoTypeInformation -Encoding UTF8

    Write-Host "`n=== $Name / account $AccountLogin ===" -ForegroundColor Cyan
    Write-Host "Deals selected: $($selected.Count) / $($rows.Count)"
    if ($Magic.Count -gt 0) {
        Write-Host "Magic: $($Magic -join ', ')"
    }
    else {
        Write-Host "Magic: all non-zero values exported by the MT5 monitor"
    }

    & $python -m trademind.grid_deal_reconstruction `
        --deals $deals `
        --output $rawLegs
    if ($LASTEXITCODE -ne 0) {
        throw "$Name reconstruction failed"
    }

    & $python -m trademind.grid_snapshot_drawdown `
        --legs $rawLegs `
        --positions $sourcePositions `
        --account-snapshots $sourceAccount `
        --output $legs `
        --summary-dir $snapshotReport
    if ($LASTEXITCODE -ne 0) {
        throw "$Name snapshot drawdown enrichment failed"
    }

    & $python -m trademind.grid_basket_audit `
        --legs $legs `
        --output-dir $ReportDir
    if ($LASTEXITCODE -ne 0) {
        throw "$Name basket audit failed"
    }
}

$aoWork = Join-Path $accountsRoot "AOExtremum_$AOAccount"
$aoReport = Join-Path $reportsRoot "AOExtremum_$AOAccount"
$multiWork = Join-Path $accountsRoot "MultiRSI_$MultiAccount"
$multiReport = Join-Path $reportsRoot "MultiRSI_$MultiAccount"

Invoke-AccountAudit `
    -Name "AOExtremum" `
    -AccountLogin $AOAccount `
    -WorkDir $aoWork `
    -ReportDir $aoReport

Invoke-AccountAudit `
    -Name "MultiRSI" `
    -AccountLogin $MultiAccount `
    -WorkDir $multiWork `
    -ReportDir $multiReport `
    -Magic $MultiMagic

$centerArgs = @(
    "-m", "trademind.robot_control_center",
    "--report", "AOExtremum|$AOAccount|$aoReport",
    "--report", "MultiRSI|$MultiAccount|$multiReport",
    "--output-dir", $root
)
if ($OpenDashboard) {
    $centerArgs += "--open-dashboard"
}

& $python @centerArgs
if ($LASTEXITCODE -ne 0) {
    throw "TradeMind control center failed"
}

Write-Host "`nControl center:" -ForegroundColor Green
Write-Host (Join-Path $root "dashboard\index.html")
Write-Host "Both MT5 monitors remain read-only. No strategy settings were changed." `
    -ForegroundColor Green
