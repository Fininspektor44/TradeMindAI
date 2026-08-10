param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path $Python)) { throw "Python not found: $Python" }

Write-Host "[1/2] Ruff: Discovery + Orchestrator bridge"
& $Python -m ruff check src\trademind\discovery src\trademind\orchestrator tests\discovery tests\orchestrator
if ($LASTEXITCODE -ne 0) { throw "Discovery-Orchestrator ruff gate failed with exit code $LASTEXITCODE" }

Write-Host "[2/2] Pytest: Discovery + Orchestrator focused suites"
$BaseTemp = Join-Path $env:TEMP ("tmai-bridge-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $BaseTemp | Out-Null
& $Python -m pytest -q -p no:cacheprovider --basetemp $BaseTemp tests\discovery tests\orchestrator
if ($LASTEXITCODE -ne 0) { throw "Discovery-Orchestrator pytest gate failed with exit code $LASTEXITCODE" }

Write-Host "DISCOVERY_ORCHESTRATOR_BRIDGE_LOCAL_GATE=PASS"
