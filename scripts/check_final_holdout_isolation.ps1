param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path $Python)) { throw "Python not found: $Python" }

Write-Host "[1/3] Dependency: authenticated holdout crypto"
& $Python -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; print('cryptography AESGCM: OK')"
if ($LASTEXITCODE -ne 0) {
    throw "cryptography dependency missing; install project dependencies before running this gate"
}

Write-Host "[2/3] Ruff: Discovery + Orchestrator isolation boundary"
& $Python -m ruff check src\trademind\discovery src\trademind\orchestrator tests\discovery tests\orchestrator
if ($LASTEXITCODE -ne 0) { throw "Final-holdout ruff gate failed with exit code $LASTEXITCODE" }

Write-Host "[3/3] Pytest: Discovery + Orchestrator focused suites"
$BaseTemp = Join-Path $env:TEMP ("tmai-holdout-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $BaseTemp | Out-Null
& $Python -m pytest -q -p no:cacheprovider --basetemp $BaseTemp tests\discovery tests\orchestrator
if ($LASTEXITCODE -ne 0) { throw "Final-holdout pytest gate failed with exit code $LASTEXITCODE" }

Write-Host "FINAL_HOLDOUT_ISOLATION_LOCAL_GATE=PASS"
