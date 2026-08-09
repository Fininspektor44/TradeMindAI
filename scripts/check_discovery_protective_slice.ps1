param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path $Python)) { throw "Python not found: $Python" }

Write-Host "[1/2] Ruff: Discovery protective slice"
& $Python -m ruff check src\trademind\discovery tests\discovery
if ($LASTEXITCODE -ne 0) { throw "Discovery ruff gate failed with exit code $LASTEXITCODE" }

Write-Host "[2/2] Pytest: Discovery protective slice"
$FocusedBaseTemp = Join-Path $env:TEMP ("tmai-disc-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $FocusedBaseTemp | Out-Null
& $Python -m pytest -q -p no:cacheprovider --basetemp $FocusedBaseTemp tests\discovery
if ($LASTEXITCODE -ne 0) { throw "Discovery pytest gate failed with exit code $LASTEXITCODE" }

Write-Host "DISCOVERY_PROTECTIVE_SLICE_LOCAL_GATE=PASS"
