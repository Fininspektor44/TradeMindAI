param(
    [string]$Login = "77053345"
)

$ErrorActionPreference = "Stop"
$common = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files"
$now = Get-Date

$expected = @(
    [pscustomobject]@{ Kind = "risk-account"; Path = Join-Path $common "TradeMindAI\mt5_risk_account_utc_$Login.csv"; MaxAgeSeconds = 90 },
    [pscustomobject]@{ Kind = "risk-positions"; Path = Join-Path $common "TradeMindAI\mt5_risk_positions_utc_$Login.csv"; MaxAgeSeconds = 90 },
    [pscustomobject]@{ Kind = "risk-symbols"; Path = Join-Path $common "TradeMindAI\mt5_risk_symbols_utc_$Login.csv"; MaxAgeSeconds = 90 },
    [pscustomobject]@{ Kind = "deal-account"; Path = Join-Path $common "TradeMindAI\grid_account_$Login.csv"; MaxAgeSeconds = 150 },
    [pscustomobject]@{ Kind = "deals"; Path = Join-Path $common "TradeMindAI\grid_deals_$Login.csv"; MaxAgeSeconds = 150 },
    [pscustomobject]@{ Kind = "deal-positions"; Path = Join-Path $common "TradeMindAI\grid_positions_$Login.csv"; MaxAgeSeconds = 150 }
)

$rows = foreach ($item in $expected) {
    $file = Get-Item $item.Path -ErrorAction SilentlyContinue
    if (-not $file) {
        [pscustomobject]@{ Kind = $item.Kind; Exists = $false; AgeSeconds = $null; Fresh = $false; Path = $item.Path }
        continue
    }
    $age = [math]::Round(($now - $file.LastWriteTime).TotalSeconds, 1)
    [pscustomobject]@{ Kind = $item.Kind; Exists = $true; AgeSeconds = $age; Fresh = ($age -le $item.MaxAgeSeconds); Path = $file.FullName }
}

$volumeDir = Join-Path $common "TradeMindAI_Volume_v1_4"
$latestVolume = Get-ChildItem $volumeDir -File -Filter "volume_*_M5.csv" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($latestVolume) {
    $volumeAge = [math]::Round(($now - $latestVolume.LastWriteTime).TotalSeconds, 1)
    $rows += [pscustomobject]@{ Kind = "volume-latest"; Exists = $true; AgeSeconds = $volumeAge; Fresh = ($volumeAge -le 420); Path = $latestVolume.FullName }
} else {
    $rows += [pscustomobject]@{ Kind = "volume-latest"; Exists = $false; AgeSeconds = $null; Fresh = $false; Path = $volumeDir }
}

$rows | Format-Table Kind,Exists,AgeSeconds,Fresh,Path -AutoSize
if (($rows | Where-Object { -not $_.Fresh }).Count -gt 0) {
    throw "One or more unified exporter outputs are missing or stale. Keep the legacy exporters attached until this is resolved."
}

Write-Host "v1.32 unified ECN outputs are fresh for login $Login" -ForegroundColor Green
Write-Host "Safe to remove the three legacy exporter EAs from their charts after visually confirming the unified EA is running." -ForegroundColor Green
