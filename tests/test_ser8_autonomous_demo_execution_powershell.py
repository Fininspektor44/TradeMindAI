"""Tests for scripts/run_ser8_autonomous_demo_execution.ps1 and
scripts/install_ser8_autonomous_demo_execution.ps1 -- SER8 AUTONOMOUS
CONTINUOUS DEMO EXECUTION V1's Windows automation.

No PowerShell interpreter is available in this environment (a standing
constraint throughout this session for every .ps1-touching task), so
every proof here is a static source-scan of the two .ps1 files, matching
the technique already established for every other PowerShell task this
session (e.g. tests/test_ser8_mt5_reconciliation_powershell_db_param_fix.py).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_ser8_autonomous_demo_execution.ps1"
WRAPPER_SCRIPT = REPO_ROOT / "scripts" / "run_ser8_autonomous_demo_execution.ps1"
PYTHON_CLI = REPO_ROOT / "scripts" / "run_ser8_autonomous_demo_execution.py"


def _executable_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.strip().startswith("#")]


# ---------------------------------------------------------------------------
# $Db is never used (the confirmed real Windows PowerShell parameter-
# binder failure this repository already fixed once and must never
# regress into a new script).
# ---------------------------------------------------------------------------


def test_neither_script_declares_a_db_parameter() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        for line in _executable_lines(text):
            assert re.search(r"\$Db\b", line) is None, f"{path.name}: unexpected $Db in {line!r}"


def test_both_scripts_declare_databasepath_parameter() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert '[string]$DatabasePath = ".\\data\\ser8_registry.db",' in text


# ---------------------------------------------------------------------------
# Installer passes every required parameter through to the wrapper; the
# wrapper forwards every one to the Python CLI's own unchanged flags.
# ---------------------------------------------------------------------------


def test_installer_passes_every_parameter_to_the_wrapper_task_command() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-DatabasePath `\"$DatabasePath`\"" in text
    assert "-HypothesisId `\"$HypothesisId`\"" in text
    assert "-Account `\"$Account`\"" in text
    assert "-DemoAccountAllowlist $allowlistArgument" in text
    assert "-RuntimeRoot `\"$RuntimeRoot`\"" in text
    assert "-Mt5ExportDir `\"$Mt5ExportDir`\"" in text
    assert "-SealedHoldoutPath `\"$SealedHoldoutPath`\"" in text
    assert "-RiskProfile `\"$RiskProfile`\"" in text
    assert "-CommonFilesDir `\"$CommonFilesDir`\"" in text
    assert "-Db " not in text
    assert "`\"$Db`\"" not in text


def test_wrapper_forwards_every_parameter_to_python_cli_unchanged_flags() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    assert '"--db", $DatabasePath,' in text
    assert '"--hypothesis-id", $HypothesisId,' in text
    assert '"--account", $Account,' in text
    assert '"--runtime-root", $RuntimeRoot,' in text
    assert '"--mt5-export-dir", $Mt5ExportDir,' in text
    assert '"--sealed-holdout-path", $SealedHoldoutPath,' in text
    assert '"--risk-profile", $RiskProfile,' in text
    assert '"--common-files-dir", $CommonFilesDir,' in text
    assert '"--demo-account-allowlist"' in text


# ---------------------------------------------------------------------------
# SER8 AUTONOMOUS WINDOWS HOLDOUT METRIC ARGUMENT FIX V1: --holdout-
# primary-metric is genuinely optional at the Python side, and neither
# PowerShell script ever bakes in a dangling/empty native argument for it
# -- PowerShell silently drops an empty-string array element when
# splatting into a NATIVE executable call (the confirmed real Windows
# root cause), so the wrapper must forward this flag ONLY when the
# operator actually set a non-empty value.
# ---------------------------------------------------------------------------


def test_wrapper_only_forwards_holdout_primary_metric_when_non_empty() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    # The flag is never baked into the unconditional base $arguments list...
    base_block = "\n".join(_executable_lines(
        text[text.index("$arguments = @("):text.index("if ($HoldoutPrimaryMetric)")]
    ))
    assert "--holdout-primary-metric" not in base_block
    # ...it is appended ONLY inside an explicit non-empty guard.
    assert "if ($HoldoutPrimaryMetric) {" in text
    guard_block = text[text.index("if ($HoldoutPrimaryMetric) {"):text.index("if ($DryRun) {")]
    assert '"--holdout-primary-metric", $HoldoutPrimaryMetric' in guard_block
    # Default wrapper invocation (the operator never sets it) therefore
    # produces valid argv with the flag entirely absent -- never a
    # dangling "--holdout-primary-metric" with no value.
    assert '[string]$HoldoutPrimaryMetric = ""' in text


def test_installer_only_forwards_holdout_primary_metric_when_non_empty() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    base_block = "\n".join(_executable_lines(
        text[text.index('$taskArguments = "'):text.index("if ($HoldoutPrimaryMetric)")]
    ))
    assert "-HoldoutPrimaryMetric" not in base_block
    assert "if ($HoldoutPrimaryMetric) {" in text
    guard_block = text[text.index("if ($HoldoutPrimaryMetric) {"):text.index("if ($DryRun) {")]
    assert "-HoldoutPrimaryMetric `\"$HoldoutPrimaryMetric`\"" in guard_block
    assert '[string]$HoldoutPrimaryMetric = ""' in text


def test_wrapper_forwards_explicit_nonempty_holdout_primary_metric() -> None:
    """An operator-supplied non-empty value is still forwarded correctly
    -- the fix only omits the flag when genuinely empty, never silently
    drops a real operator-supplied value."""
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    guard_block = text[text.index("if ($HoldoutPrimaryMetric) {"):text.index("if ($DryRun) {")]
    assert '$arguments += @("--holdout-primary-metric", $HoldoutPrimaryMetric)' in guard_block


def test_python_cli_flags_themselves_are_unchanged() -> None:
    cli_source = PYTHON_CLI.read_text(encoding="utf-8")
    for flag in (
        '"--db"', '"--hypothesis-id"', '"--account"', '"--demo-account-allowlist"', '"--runtime-root"',
        '"--mt5-export-dir"', '"--sealed-holdout-path"', '"--holdout-primary-metric"', '"--risk-profile"',
        '"--common-files-dir"', '"--dry-run"', '"--once"',
    ):
        assert flag in cli_source
    assert "DatabasePath" not in cli_source  # the Python CLI itself never needed renaming.


# ---------------------------------------------------------------------------
# Native ScheduledTasks registration; never schtasks.exe /TR; never
# RepetitionDuration/MaxValue.
# ---------------------------------------------------------------------------


def test_installer_uses_native_scheduledtasks_module_and_indefinite_repetition() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert '[string]$TaskName = "TradeMindAI-SER8-Autonomous-Demo-Execution"' in text
    assert "[int]$IntervalMinutes = 1" in text
    assert '[string]$Account = "67206924"' in text
    assert "New-ScheduledTaskAction" in text
    assert "New-ScheduledTaskTrigger" in text
    assert "New-ScheduledTaskPrincipal" in text
    assert "Register-ScheduledTask" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel Highest" in text
    assert "-Force" in text
    assert "[switch]$DryRun" in text
    assert "[switch]$Remove" in text
    for line in _executable_lines(text):
        assert "schtasks.exe" not in line
        assert "/TR" not in line
        assert "-RepetitionDuration" not in line
        assert "MaxValue" not in line
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in text


def test_installer_prints_every_required_status_field() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    for label in ("TaskName:", "Account:", "RuntimeRoot:", "RiskProfile:", "DatabasePath:", "DryRun:", "Interval:", "LogPath:"):
        assert f'Write-Host "{label}' in text, label


# ---------------------------------------------------------------------------
# Single-instance safety: Global Mutex + internal lock file + --once
# always present, never conditional.
# ---------------------------------------------------------------------------


def test_wrapper_uses_global_mutex_and_always_passes_once() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    assert 'Global\\TradeMindAI-SER8-Autonomous-Demo-Execution-$Account' in text
    assert "New-Object System.Threading.Mutex" in text
    assert '"--once"' in text
    assert 'if ($DryRun) {' in text
    assert '$arguments += "--dry-run"' in text
    # The $arguments assignment spans two @(...) segments joined with
    # $DemoAccountAllowlist -- take the whole block up to the "if
    # ($DryRun)" that follows it, never just the first ")".
    start = text.index("$arguments = @(")
    end = text.index("if ($DryRun)", start)
    arguments_block = text[start:end]
    assert '"--once"' in arguments_block  # never conditional.


# ---------------------------------------------------------------------------
# No trading/broker execution path in either script.
# ---------------------------------------------------------------------------


def test_no_trading_or_broker_execution_path_introduced() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("OrderSend", "trade.Buy(", "trade.Sell(", "MetaTrader5", "CTrade"):
            assert forbidden not in text


# ---------------------------------------------------------------------------
# Sanity: manual brace/paren balance (no PowerShell interpreter available).
# ---------------------------------------------------------------------------


def test_both_scripts_braces_and_parens_balanced() -> None:
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert text.count("{") == text.count("}"), path
        assert text.count("(") == text.count(")"), path


# ---------------------------------------------------------------------------
# SER8 FULL SYMBOL UNIVERSE + RESEARCH RANKING V1: -HypothesisIds is
# purely additive, never a dangling empty native argument, and never
# clashes with the proven single-hypothesis $HypothesisId default.
# ---------------------------------------------------------------------------


def test_both_scripts_default_to_exact_core8_operational_routes() -> None:
    expected = (
        "core8-op:CHFJPY", "core8-op:EURJPY", "core8-op:EURNZD", "core8-op:GBPAUD",
        "core8-op:GBPNZD", "core8-op:NZDCAD", "core8-op:NZDCHF", "core8-op:USDJPY",
    )
    for path in (INSTALL_SCRIPT, WRAPPER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        start = text.index("[string[]]$HypothesisIds = @(")
        block = text[start:text.index("),", start)]
        assert tuple(re.findall(r'"(core8-op:[A-Z]+)"', block)) == expected
        assert "core8-op:EURUSD" not in text


def test_one_mutex_covers_producer_then_execution_then_reconciliation() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    producer = text.index('"run_v121_live_signal_runtime.ps1"')
    execution = text.index("& $python @arguments")
    reconciliation = text.index('"reconcile_ser8_mt5_execution.py"')
    assert text.index("try {") < producer < execution < reconciliation < text.index("finally {")
    assert '[string]$MarketDataAccount = "77053345"' in text
    assert '[string]$Account = "67206924"' in text
    assert "TradeMindAI_Volume_v1_4" in text


def test_installer_disables_old_independent_runtime_and_reconciliation_tasks() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    for task_name in (
        "TradeMindAI-v1.32-ECN-LiveSignalRuntime",
        "TradeMindAI-v1.21-LiveSignalRuntime",
        "TradeMindAI-SER8-MT5-Reconciliation",
    ):
        assert task_name in text
    assert "Disable-ScheduledTask" in text


def test_wrapper_dispatches_to_hypothesis_ids_only_when_non_empty() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    assert "$usingMultiHypothesis = $HypothesisIds.Count -gt 0" in text
    assert 'if ($usingMultiHypothesis) {' in text
    assert '"--hypothesis-ids"' in text
    # The single-hypothesis $arguments block (the else branch) still
    # forwards --hypothesis-id exactly as before -- never dropped.
    assert '"--hypothesis-id", $HypothesisId,' in text


def test_wrapper_never_forwards_both_hypothesis_flags_in_one_arguments_block() -> None:
    text = WRAPPER_SCRIPT.read_text(encoding="utf-8")
    start = text.index("$arguments = @(")
    start = text.rindex("if ($usingMultiHypothesis) {", 0, start)
    end = text.index("if ($DryRun) {", start)
    dispatch_block = text[start:end]
    multi_branch, _, single_branch = dispatch_block.partition("else {")
    assert "--hypothesis-ids" in multi_branch and "--hypothesis-id" not in multi_branch.replace("--hypothesis-ids", "")
    assert "--hypothesis-id" in single_branch and "--hypothesis-ids" not in single_branch


def test_installer_only_forwards_hypothesis_ids_when_non_empty() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "if ($HypothesisIds.Count -gt 0) {" in text
    guard_block = text[text.index("if ($HypothesisIds.Count -gt 0) {"):text.index("if ($DryRun) {")]
    assert "-HypothesisIds $hypothesisIdsArgument" in guard_block
    # -HypothesisId (singular) is still unconditionally baked into the
    # base task arguments -- the wrapper's own dispatch decides which one
    # actually wins, matching the Python CLI's exactly-one-of validation.
    base_block = text[text.index('$taskArguments = "'):text.index("if ($HoldoutPrimaryMetric)")]
    assert "-HypothesisId `\"$HypothesisId`\"" in base_block
    assert "-HypothesisIds" not in base_block
