"""Tests for SER8 RECONCILIATION POWERSHELL DB PARAMETER FIX V1.

Real Windows failure this task fixes:

    ParameterBindingException:
    "Не удается задать параметр 'Db' из-за конфликта с псевдонимом
    параметра 'Debug'."

Both scripts/run_ser8_mt5_reconciliation.ps1 and scripts/
install_ser8_mt5_reconciliation.ps1 declared an advanced PowerShell
parameter named ``$Db``, which PowerShell's own parameter binder treated
as ambiguous against the common parameter ``-Debug``, blocking Scheduled
Task installation entirely. The fix renames the parameter to
``DatabasePath`` in both scripts -- a name that cannot collide with any
PowerShell common parameter -- while leaving the Python CLI's own
``--db`` flag name completely unchanged (only the PowerShell-side
variable name changed).

No PowerShell interpreter is available in this environment (a standing
constraint throughout this session for every .ps1-touching task), so
every proof here is a static source-scan of the two .ps1 files, matching
the technique already established for every other PowerShell task this
session (e.g. tests/test_ser8_scheduled_task_long_command_fix.py).

This file does not import test helpers from sibling test files (consistent
with this session's own established convention).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_ser8_mt5_reconciliation.ps1"
WRAPPER_SCRIPT = REPO_ROOT / "scripts" / "run_ser8_mt5_reconciliation.ps1"
PYTHON_CLI = REPO_ROOT / "scripts" / "reconcile_ser8_mt5_execution.py"


def _executable_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.strip().startswith("#")]


# ---------------------------------------------------------------------------
# 1: neither PS1 exposes a parameter named Db; both expose DatabasePath.
# ---------------------------------------------------------------------------


def test_neither_script_declares_a_db_parameter() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        for line in _executable_lines(text):
            # A bare "$Db" parameter/variable token -- word-boundaried so
            # this does not false-positive on "$DatabasePath" itself.
            assert re.search(r"\$Db\b", line) is None, f"{path.name}: unexpected $Db in {line!r}"


def test_both_scripts_declare_databasepath_parameter() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert '[string]$DatabasePath = ".\\data\\ser8_registry.db",' in text


# ---------------------------------------------------------------------------
# 2-4: installer passes -DatabasePath to the wrapper; the wrapper passes
# DatabasePath through to the Python CLI's own unchanged --db flag.
# ---------------------------------------------------------------------------


def test_installer_passes_databasepath_to_the_wrapper_task_command() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'\$taskArguments = "(.*)"\s*\n', text)
    assert match, "expected a single-line $taskArguments assignment"
    command_literal = match.group(1)
    assert "-DatabasePath `\"$DatabasePath`\"" in command_literal
    assert "-Db " not in command_literal
    assert "`\"$Db`\"" not in command_literal


def test_wrapper_forwards_databasepath_to_python_db_flag() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    assert '"--db", $DatabasePath,' in text


def test_python_cli_db_flag_itself_is_unchanged() -> None:
    # Requirement 5: the Python CLI must still be invoked with --db -- only
    # the PowerShell-side variable feeding it was renamed.
    cli_source = PYTHON_CLI.read_text(encoding="utf-8")
    assert '"--db"' in cli_source
    assert "DatabasePath" not in cli_source  # the Python CLI itself never needed renaming.


# ---------------------------------------------------------------------------
# 7 (search the repository): every other invocation of these two scripts
# also uses -DatabasePath, never -Db.
# ---------------------------------------------------------------------------


def test_no_remaining_db_flag_usage_anywhere_in_the_repository() -> None:
    for path in REPO_ROOT.rglob("*.ps1"):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in _executable_lines(text):
            assert re.search(r"-Db\b", line) is None, f"{path}: unexpected -Db usage in {line!r}"
    for path in REPO_ROOT.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in _executable_lines(text):
            if "argparse" in line or "--db" in line:
                continue
            assert re.search(r"[\"']-Db[\"']", line) is None, f"{path}: unexpected -Db usage in {line!r}"


# ---------------------------------------------------------------------------
# 6/8: everything else preserved -- TaskName, Account, Mt5ExportDir,
# DryRun, interval semantics, S4U/RunLevel Highest, Global Mutex, native
# ScheduledTasks registration, no RepetitionDuration, no schtasks.exe /TR.
# ---------------------------------------------------------------------------


def test_installer_preserves_every_other_parameter_and_registration_semantics() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert '[string]$TaskName = "TradeMindAI-SER8-MT5-Reconciliation"' in text
    assert "[int]$IntervalMinutes = 1" in text
    assert '[string]$Account = "67206924"' in text
    assert '[string]$Mt5ExportDir = ".\\data\\mt5"' in text
    assert "[switch]$DryRun" in text
    assert "[switch]$Remove" in text
    assert "New-ScheduledTaskAction" in text
    assert "New-ScheduledTaskTrigger" in text
    assert "New-ScheduledTaskPrincipal" in text
    assert "Register-ScheduledTask" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel Highest" in text
    assert "-Force" in text
    # No schtasks.exe /TR regression, no RepetitionDuration/MaxValue regression.
    for line in _executable_lines(text):
        assert "schtasks.exe" not in line
        assert "/TR" not in line
        assert "-RepetitionDuration" not in line
        assert "MaxValue" not in line
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in text


def test_wrapper_preserves_mutex_and_lock_file_and_once_invocation() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    assert 'Global\\TradeMindAI-SER8-MT5-Reconciliation-$Account' in text
    assert "New-Object System.Threading.Mutex" in text
    assert '"--once"' in text
    assert '"--mt5-export-dir", $Mt5ExportDir,' in text
    assert '"--account", $Account,' in text
    assert 'if ($DryRun) {' in text
    assert '$arguments += "--dry-run"' in text


# ---------------------------------------------------------------------------
# 8: no trading/broker execution path is introduced by this fix.
# ---------------------------------------------------------------------------


def test_no_trading_or_broker_execution_path_introduced() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("OrderSend", "trade.Buy(", "trade.Sell(", "MetaTrader5", "CTrade"):
            assert forbidden not in text


def test_wrapper_never_bypasses_the_dry_run_or_once_flags() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    # --once is always present in the base argument list (never
    # conditional), so a scheduled/looping invocation can never
    # accidentally run the Python CLI's own continuous-loop mode.
    arguments_block = text[text.index("$arguments = @("):text.index(")", text.index("$arguments = @("))]
    assert '"--once"' in arguments_block


# ---------------------------------------------------------------------------
# Sanity: manual brace/paren balance (no PowerShell interpreter available).
# ---------------------------------------------------------------------------


def test_both_scripts_braces_and_parens_balanced() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert text.count("{") == text.count("}"), path
        assert text.count("(") == text.count(")"), path
