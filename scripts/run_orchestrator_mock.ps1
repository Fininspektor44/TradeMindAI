param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path $Python)) { throw "Python not found: $Python" }

& $Python -m trademind.orchestrator.mock_runner --repo-root $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "Orchestrator mock validation failed with exit code $LASTEXITCODE" }
