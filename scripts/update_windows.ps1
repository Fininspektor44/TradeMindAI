param(
    [string]$TaskName = "TradeMindAI Live"
)

$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
Set-Location $projectPath

Write-Host "Stopping old TradeMind watcher processes..."
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*run_live.ps1*" } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "Stopped PID $($_.ProcessId) $($_.Name)"
        }
        catch {
            Write-Warning "Could not stop PID $($_.ProcessId): $($_.Exception.Message)"
        }
    }

$python = Join-Path $projectPath ".venv\Scripts\python.exe"
$ruff = Join-Path $projectPath ".venv\Scripts\ruff.exe"

if (-not (Test-Path $python -PathType Leaf)) {
    throw "Virtual-environment Python not found: $python"
}

Write-Host "Installing current TradeMind source..."
& $python -m pip install ".[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed with exit code $LASTEXITCODE"
}

Write-Host "Running tests..."
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "pytest failed with exit code $LASTEXITCODE"
}

Write-Host "Running Ruff..."
& $ruff check .
if ($LASTEXITCODE -ne 0) {
    throw "ruff failed with exit code $LASTEXITCODE"
}

Write-Host "Starting scheduled TradeMind watcher..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8

$processes = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*run_live.ps1*" } |
    Select-Object ProcessId, Name, CommandLine

if (-not $processes) {
    throw "TradeMind watcher did not start. Check logs\live.log and Task Scheduler."
}

Write-Host "TradeMind update completed successfully."
$processes | Format-Table -AutoSize

$logFile = Join-Path $projectPath "logs\live.log"
if (Test-Path $logFile -PathType Leaf) {
    Write-Host "`nLatest live log lines:"
    Get-Content $logFile -Tail 20
}
