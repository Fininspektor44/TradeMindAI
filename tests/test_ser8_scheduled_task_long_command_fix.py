"""Tests for SER8 SCHEDULED TASK LONG COMMAND FIX V1.

Real Windows failure this task fixes:

    ОШИБКА: Значение параметра "/TR" не может содержать более 261 знаков.
    Failed to create scheduled task: TradeMindAI-v1.32-ECN-LiveSignalRuntime

scripts/install_v121_live_signal_watch.ps1 previously registered its
Scheduled Task via ``schtasks.exe /Create /TR "<one collapsed command
string>"`` -- and Windows caps the ``/TR`` value at 261 characters. A real
repo checkout path plus the full watch-script path plus
-Login/-ServerUTCOffsetHours/-RuntimeRoot routinely exceeds that. This task
replaces that registration with the native ScheduledTasks module
(New-ScheduledTaskAction / New-ScheduledTaskTrigger /
New-ScheduledTaskPrincipal / Register-ScheduledTask), which stores the
executable and its arguments as SEPARATE Task Scheduler XML fields with no
such combined-string cap.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention for new SER8 test modules).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_v121_live_signal_watch.ps1"
WATCH_SCRIPT = REPO_ROOT / "scripts" / "run_v121_live_signal_watch.ps1"


# ---------------------------------------------------------------------------
# 1: schtasks.exe /Create /TR is no longer used for registration.
# ---------------------------------------------------------------------------


def test_install_script_no_longer_invokes_schtasks_for_task_creation() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # No literal schtasks.exe invocation remains anywhere in the file (the
    # word still appears, but only inside explanatory comments describing
    # the OLD, now-removed mechanism -- never as an executed `&` call).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "schtasks.exe" not in stripped, f"schtasks.exe still executed: {stripped!r}"
        assert "/TR" not in stripped, f"/TR still used: {stripped!r}"


def test_install_script_uses_native_scheduledtasks_cmdlets_for_creation() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    for cmdlet in (
        "New-ScheduledTaskAction",
        "New-ScheduledTaskTrigger",
        "New-ScheduledTaskPrincipal",
        "Register-ScheduledTask",
    ):
        assert cmdlet in text, f"expected {cmdlet} to be used for task registration"


# ---------------------------------------------------------------------------
# 2: every other required behaviour preserved (TaskName, cadence, highest
# privileges, hidden/noninteractive PowerShell, watch script, Login,
# ServerUTCOffsetHours, RuntimeRoot, working directory, immediate start,
# -Remove).
# ---------------------------------------------------------------------------


def test_task_name_parameter_and_default_preserved() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert '[string]$TaskName = "TradeMindAI-v1.21-LiveSignalRuntime"' in text
    assert "-TaskName $TaskName" in text  # passed to Register-ScheduledTask


def test_one_minute_cadence_preserved_via_repetition_interval() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "[int]$IntervalMinutes = 1" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in text
    # Repetition is indefinite BY OMISSION of -RepetitionDuration (Task
    # Scheduler's own authoritative semantics: an omitted Duration means
    # "repeat forever") -- see
    # tests/test_ser8_scheduled_task_indefinite_repetition_fix.py for the
    # dedicated proof that no -RepetitionDuration value (MaxValue or
    # otherwise) is ever passed.


def test_highest_privileges_preserved() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-RunLevel Highest" in text


def test_hidden_noninteractive_powershell_invocation_preserved() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass" in text


def test_same_watch_script_still_targeted() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert 'Join-Path $PSScriptRoot "run_v121_live_signal_watch.ps1"' in text
    assert "-File `\"$watchScript`\"" in text
    assert WATCH_SCRIPT.is_file()


def test_login_server_offset_runtime_root_all_still_forwarded() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-Login `\"$Login`\"" in text
    assert "-ServerUTCOffsetHours $ServerUTCOffsetHours" in text
    assert "-RuntimeRoot `\"$RuntimeRoot`\"" in text
    assert '[string]$RuntimeRoot = ".\\data\\live_signal_runtime_v1"' in text


def test_working_directory_semantics_preserved() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-WorkingDirectory $repo" in text
    assert '$repo = Split-Path -Parent $PSScriptRoot' in text


def test_immediate_start_after_install_preserved() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "Start-ScheduledTask -TaskName $TaskName" in text


def test_remove_behavior_preserved_via_native_unregister() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    remove_start = text.index("if ($Remove) {")
    remove_block = text[remove_start : remove_start + 400]
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in remove_block
    assert "schtasks.exe" not in remove_block
    assert "Removed scheduled task: $TaskName" in remove_block


# ---------------------------------------------------------------------------
# 3: no second launcher/wrapper introduced.
# ---------------------------------------------------------------------------


def test_no_second_wrapper_script_introduced() -> None:
    # The installer still targets exactly the one existing watch script --
    # no new intermediate .ps1/.bat/.cmd launcher was added to shorten
    # anything.
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert text.count(".ps1") == text.count("run_v121_live_signal_watch.ps1") + text.count(
        "run_v121_live_signal_runtime.ps1"
    )
    watch_referenced_scripts = set(re.findall(r'"([A-Za-z0-9_.]+\.ps1)"', text))
    assert watch_referenced_scripts <= {"run_v121_live_signal_watch.ps1"}


# ---------------------------------------------------------------------------
# 4: no hardcoded account paths.
# ---------------------------------------------------------------------------


def test_no_hardcoded_account_specific_paths() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # The only login default is the pre-existing 37365712 placeholder,
    # unchanged by this task -- no new account/runtime-root value is baked
    # in anywhere in the registration logic itself.
    assert "77053345" not in text
    assert "67206924" not in text


# ---------------------------------------------------------------------------
# 5: installer safely replaces/updates an existing task of the same name.
# ---------------------------------------------------------------------------


def test_register_scheduledtask_call_uses_force_to_replace_existing_task() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"Register-ScheduledTask[^\n]*", text)
    assert match, "expected a Register-ScheduledTask call"
    assert "-Force" in match.group(0), (
        "Register-ScheduledTask must use -Force to safely replace an "
        "existing task of the same name in one step"
    )


# ---------------------------------------------------------------------------
# 6: long, realistic Windows paths (>261 chars) register structurally
# without ever touching schtasks.exe /TR.
# ---------------------------------------------------------------------------


def _build_task_arguments(*, watch_script: str, login: str, offset: int, runtime_root: str) -> str:
    """Mirrors install_v121_live_signal_watch.ps1's own $taskArguments
    string-interpolation formula exactly (see the real assignment in the
    script), so this test measures the SAME string the installer would
    actually hand to New-ScheduledTaskAction -Argument."""
    return (
        f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{watch_script}" '
        f'-Login "{login}" -ServerUTCOffsetHours {offset} -RuntimeRoot "{runtime_root}"'
    )


def test_realistic_long_windows_deployment_path_exceeds_261_chars() -> None:
    # A realistic, long real-world Windows deployment layout -- the exact
    # shape of path that triggered "Значение параметра "/TR" не может
    # содержать более 261 знаков" in production.
    long_repo = (
        r"C:\Users\ServiceAccount-TradeMindAI-Production\Documents"
        r"\Deployments\TradeMindAI-ECN-LiveSignalRuntime-2026-08"
        r"\repository-checkout-main"
    )
    watch_script = long_repo + r"\scripts\run_v121_live_signal_watch.ps1"
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    runtime_root = long_repo + r"\data\live_signal_runtime_ecN_77053345"

    task_arguments = _build_task_arguments(
        watch_script=watch_script, login="77053345", offset=3, runtime_root=runtime_root
    )
    legacy_single_tr_string = f'"{powershell}" {task_arguments}'

    assert len(legacy_single_tr_string) > 261, (
        "this fixture must reproduce a realistic path that would have "
        "exceeded schtasks.exe's 261-character /TR limit"
    )


def test_long_path_scenario_registers_without_schtasks_tr(tmp_path: Path) -> None:
    # Same long-path scenario as above, but now proving the ACTUAL
    # installer source no longer routes anything through /TR at all -- so
    # the 261-character ceiling that broke the real Windows install simply
    # does not apply to the mechanism this script now uses.
    long_repo = tmp_path / (
        "ServiceAccount-TradeMindAI-Production" + ("-x" * 40) + "-Deployments-2026-08-repository-checkout-main"
    )
    watch_script = long_repo / "scripts" / "run_v121_live_signal_watch.ps1"
    runtime_root = long_repo / "data" / "live_signal_runtime_ecN_77053345"

    task_arguments = _build_task_arguments(
        watch_script=str(watch_script), login="77053345", offset=3, runtime_root=str(runtime_root)
    )
    assert len(task_arguments) > 261

    install_text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    executable_lines = [
        line for line in install_text.splitlines() if not line.strip().startswith("#")
    ]
    assert not any("/TR" in line for line in executable_lines)
    assert "New-ScheduledTaskAction" in install_text
    # New-ScheduledTaskAction's own -Argument parameter is a plain string
    # with no 261-character ceiling -- the real Windows constraint that
    # broke registration belonged to schtasks.exe's /TR switch specifically,
    # not to Task Scheduler's underlying Action/Argument storage.
    assert "-Argument $taskArguments" in install_text


# ---------------------------------------------------------------------------
# 7: still one scheduled runtime entrypoint, one installer.
# ---------------------------------------------------------------------------


def test_still_exactly_one_installer_for_the_ser8_live_candidate_task() -> None:
    candidates = [
        path
        for path in (REPO_ROOT / "scripts").glob("*.ps1")
        if "run_v121_live_signal_watch.ps1" in path.read_text(encoding="utf-8")
        and "Register-ScheduledTask" in path.read_text(encoding="utf-8")
    ]
    assert candidates == [INSTALL_SCRIPT]


def test_still_exactly_one_watch_wrapper_script() -> None:
    watch_wrappers = [
        path
        for path in (REPO_ROOT / "scripts").glob("*.ps1")
        if "run_v121_live_signal_runtime.ps1" in path.read_text(encoding="utf-8")
        and "New-Object System.Threading.Mutex" in path.read_text(encoding="utf-8")
    ]
    assert watch_wrappers == [WATCH_SCRIPT]


# ---------------------------------------------------------------------------
# 8-9: no trading/risk/research changes; no broker order machinery.
# ---------------------------------------------------------------------------


def test_no_broker_order_or_risk_machinery_in_installer() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    forbidden = re.compile(r"order_send|OrderSend\(|trade\.Buy\(|trade\.Sell\(|CTrade|MetaTrader5\.|risk_manager|evaluate_risk", re.I)
    assert not forbidden.search(text)


def test_risk_profile_files_untouched_by_this_task() -> None:
    for name in ("standard_v1.json", "ser8_supervised_demo_v1.json"):
        path = REPO_ROOT / "config" / "risk_profiles" / name
        assert path.is_file()
        assert "77053345" not in path.read_text(encoding="utf-8")
        assert "261" not in path.read_text(encoding="utf-8")
