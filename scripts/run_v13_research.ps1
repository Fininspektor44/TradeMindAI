param(
    [string]$Journal = ".\data\journal_ecn\signals.csv",
    [string]$Config = ".\config\paper_gate_v1.3.json"
)

$ErrorActionPreference = "Stop"

& ".\.venv\Scripts\trademind-action-validate.exe" `
    --journal $Journal `
    --output ".\data\action_validation\latest.csv"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& ".\.venv\Scripts\trademind-paper-gate.exe" `
    --journal $Journal `
    --config $Config `
    --output ".\data\paper_signals\signals.csv" `
    --status-output ".\data\paper_signals\gate_status.csv"
exit $LASTEXITCODE
