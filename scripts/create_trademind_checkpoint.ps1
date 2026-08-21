$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "create_trademind_checkpoint.py"

if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}
if (-not (Test-Path $script)) {
    throw "Checkpoint command not found: $script"
}

# Array splatting preserves each operator-supplied argument exactly,
# including quoted layer names, paths, notes, and repeated metadata flags.
& $python $script @args
exit $LASTEXITCODE
