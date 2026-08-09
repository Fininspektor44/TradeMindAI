param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path $Python)) { throw "Python not found: $Python" }

Write-Host "[1/3] Ruff: Orchestrator v1"
& $Python -m ruff check src\trademind\orchestrator tests\orchestrator
if ($LASTEXITCODE -ne 0) { throw "Orchestrator ruff gate failed with exit code $LASTEXITCODE" }

Write-Host "[2/3] Pytest: focused Orchestrator suite"
& $Python -m pytest -q tests\orchestrator
if ($LASTEXITCODE -ne 0) { throw "Orchestrator pytest gate failed with exit code $LASTEXITCODE" }

Write-Host "[3/3] End-to-end deterministic mock cycle"
& $Python -m trademind.orchestrator.mock_runner --repo-root $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "Orchestrator mock gate failed with exit code $LASTEXITCODE" }

Write-Host "ORCHESTRATOR_V1_LOCAL_GATE=PASS"
