param(
    [Parameter(Mandatory = $false)]
    [long]$AccountLogin = 37353316
)

$ErrorActionPreference = 'Stop'

$source = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI\grid_deals_$AccountLogin.csv"
if (-not (Test-Path -LiteralPath $source)) {
    throw "Grid deal file not found: $source"
}

$rows = @(Import-Csv -LiteralPath $source | Sort-Object { [long]$_.time_msc })
if ($rows.Count -eq 0) {
    Write-Host "No deals found for account $AccountLogin" -ForegroundColor Yellow
    exit 0
}

function Convert-Time([string]$milliseconds) {
    return [DateTimeOffset]::FromUnixTimeMilliseconds([long]$milliseconds).ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss')
}

Write-Host "`n=== GRID SOURCE INSPECTION ===" -ForegroundColor Cyan
Write-Host "Account: $AccountLogin"
Write-Host "File:    $source"
Write-Host "Deals:   $($rows.Count)"
Write-Host "First:   $(Convert-Time $rows[0].time_msc)  $($rows[0].symbol)  $($rows[0].comment)" -ForegroundColor Green
Write-Host "Last:    $(Convert-Time $rows[-1].time_msc)  $($rows[-1].symbol)  $($rows[-1].comment)" -ForegroundColor Green

$report = $rows |
    Group-Object magic |
    Sort-Object Count -Descending |
    ForEach-Object {
        $group = @($_.Group | Sort-Object { [long]$_.time_msc })
        [pscustomobject]@{
            Magic   = $_.Name
            Deals   = $_.Count
            First   = Convert-Time $group[0].time_msc
            Last    = Convert-Time $group[-1].time_msc
            Symbols = (($group.symbol | Sort-Object -Unique) -join ',')
            Comment = (($group.comment | Where-Object { $_ } | Select-Object -First 1))
        }
    }

$report | Format-Table -Wrap -AutoSize
