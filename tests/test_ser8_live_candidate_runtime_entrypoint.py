"""Tests for SER8 LIVE CANDIDATE RUNTIME SINGLE ENTRYPOINT V1.

Proves, using the real files this task touched (never a rewritten copy or a
mock):

  * scripts/run_v121_live_signal_watch.ps1 is the ONE production PowerShell
    entrypoint for SER8 live candidate generation, and its own param()
    block declares every parameter scripts/install_v121_live_signal_watch.ps1
    (the ONE installer/updater for its Windows Scheduled Task) passes it --
    the real Windows failure this task fixes (-RuntimeRoot rejected by the
    wrapper, LastTaskResult=1) cannot recur.
  * scripts/run_ser8_real_demo_pipeline.py's discover_inputs() now resolves
    exactly ONE canonical live candidates.jsonl location by default
    (<data-root>/live_signal_runtime_v1/candidates.jsonl, the SAME default
    runtime root the PowerShell entrypoints use), and never silently falls
    back to the historical/research data/signal_intelligence_v1_16/ archive
    even when that historical file is the only one present -- or present
    alongside the live one.
  * the incremental v1.22.1 watermark logic (trademind.live_signal_runtime's
    own per-symbol watermarks, reused by live_signal_runtime_v122) is
    untouched by this task.
  * candidate/account signal-freshness stays at 900 seconds in both shipped
    risk profiles.
  * no broker order-send machinery exists anywhere in the live ingest
    scripts/modules this task touched.
  * the market-data account (77053345) vs demo-execution account (67206924)
    separation is preserved and explicitly documented, never silently
    changed.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention for new SER8 test modules).
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

pipeline_module = importlib.import_module("run_ser8_real_demo_pipeline")

WATCH_SCRIPT = REPO_ROOT / "scripts" / "run_v121_live_signal_watch.ps1"
RUNTIME_SCRIPT = REPO_ROOT / "scripts" / "run_v121_live_signal_runtime.ps1"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_v121_live_signal_watch.ps1"
LIVE_RUNTIME_MODULE = REPO_ROOT / "src" / "trademind" / "live_signal_runtime.py"
LIVE_RUNTIME_V122_MODULE = REPO_ROOT / "src" / "trademind" / "live_signal_runtime_v122.py"
STANDARD_PROFILE = REPO_ROOT / "config" / "risk_profiles" / "standard_v1.json"
SUPERVISED_PROFILE = REPO_ROOT / "config" / "risk_profiles" / "ser8_supervised_demo_v1.json"
V121_DOC = REPO_ROOT / "docs" / "v1.21-live-signal-runtime.md"

_MT5_ACCOUNT = "67206924"
_SYMBOL = "XAUUSD"


# ---------------------------------------------------------------------------
# Helpers for parsing the real .ps1 sources (source-scan pattern established
# elsewhere in this repository's tests for non-Python artifacts).
# ---------------------------------------------------------------------------


def _param_block(text: str) -> str:
    match = re.search(r"param\s*\(\n(.*?)\n\)\n", text, re.S)
    assert match, "expected a top-level param(...) block"
    return match.group(1)


def _declared_param_names(text: str) -> set[str]:
    block = _param_block(text)
    names = set(re.findall(r"\[Parameter[^\]]*\]\s*\n?\s*\[[A-Za-z]+\]\$(\w+)", block))
    names |= set(re.findall(r"^\s*\[switch\]\$(\w+)", block, re.M))
    assert names, "expected at least one declared parameter"
    return names


def _params_passed_to_watch_script(install_text: str) -> set[str]:
    # Since SER8 SCHEDULED TASK LONG COMMAND FIX V1, the installer builds
    # $taskArguments (a New-ScheduledTaskAction -Argument value, registered
    # via Register-ScheduledTask -- never a single schtasks.exe /TR string,
    # which is exactly what this task's fix removes).
    match = re.search(r'\$taskArguments = "(.*)"\s*\n', install_text)
    assert match, "expected a single-line $taskArguments assignment"
    cmd_literal = match.group(1)
    tail = cmd_literal.split("$watchScript", 1)[1]
    return set(re.findall(r"-([A-Za-z]+)\b", tail))


# ---------------------------------------------------------------------------
# 1-4: one scheduled entrypoint, one installer, no unsupported parameters.
# ---------------------------------------------------------------------------


def test_exactly_one_scheduled_task_installer_script_exists() -> None:
    # The repository has many unrelated Scheduled Task installers for other
    # subsystems (v142 fx_research, v160 unified_center, watchdogs, etc. --
    # see the TRADEMIND LEGACY RUNTIME PURGE audit); "one installer" means
    # one installer FOR THE SER8 LIVE-CANDIDATE watch script specifically.
    # Since SER8 SCHEDULED TASK LONG COMMAND FIX V1, task creation goes
    # through the native ScheduledTasks module (Register-ScheduledTask), not
    # schtasks.exe /Create -- see
    # tests/test_ser8_scheduled_task_long_command_fix.py for the dedicated
    # proof of that migration.
    creators = [
        path
        for path in (REPO_ROOT / "scripts").glob("*.ps1")
        if "run_v121_live_signal_watch.ps1" in path.read_text(encoding="utf-8")
        and ("Register-ScheduledTask" in path.read_text(encoding="utf-8") or "schtasks.exe" in path.read_text(encoding="utf-8"))
    ]
    assert creators == [INSTALL_SCRIPT]


def test_watch_script_declares_every_parameter_the_installer_passes() -> None:
    install_text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    watch_text = WATCH_SCRIPT.read_text(encoding="utf-8")
    passed = _params_passed_to_watch_script(install_text)
    declared = _declared_param_names(watch_text)
    assert passed == {"Login", "ServerUTCOffsetHours", "RuntimeRoot"}
    assert passed <= declared, (
        f"installer passes {passed - declared} which run_v121_live_signal_watch.ps1's "
        "own param() block does not declare -- this is exactly the real Windows "
        "LastTaskResult=1 failure mode this task fixes"
    )


def test_watch_script_declares_runtime_root_with_the_shared_default() -> None:
    watch_text = WATCH_SCRIPT.read_text(encoding="utf-8")
    assert '[string]$RuntimeRoot = ".\\data\\live_signal_runtime_v1"' in watch_text


def test_install_script_always_forwards_runtime_root_into_task_command() -> None:
    install_text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-RuntimeRoot" in install_text
    assert '[string]$RuntimeRoot = ".\\data\\live_signal_runtime_v1"' in install_text


def test_watch_script_forwards_runtime_root_to_the_real_runtime_script() -> None:
    watch_text = WATCH_SCRIPT.read_text(encoding="utf-8")
    # The actual delegated invocation (not the explanatory comment above the
    # param() block, which also mentions the script name).
    call_marker = '& (Join-Path $PSScriptRoot "run_v121_live_signal_runtime.ps1")'
    assert call_marker in watch_text
    call_start = watch_text.index(call_marker)
    call_region = watch_text[call_start : call_start + 400]
    assert "-RuntimeRoot $RuntimeRoot" in call_region


def test_runtime_script_itself_already_supports_runtime_root() -> None:
    # Audited, unmodified by this task: run_v121_live_signal_runtime.ps1
    # already accepted -RuntimeRoot before this fix -- the gap was only in
    # the watch wrapper and its installer.
    runtime_text = RUNTIME_SCRIPT.read_text(encoding="utf-8")
    assert "$RuntimeRoot" in runtime_text
    assert "--runtime-root" in runtime_text


# ---------------------------------------------------------------------------
# 5-8: one canonical live candidate journal; historical archive never a
# silent live-execution default.
# ---------------------------------------------------------------------------


def _write_mt5_exports(mt5_dir: Path, *, account: str = _MT5_ACCOUNT) -> None:
    import csv as _csv

    mt5_dir.mkdir(parents=True, exist_ok=True)
    captured_msc = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp() * 1000)

    def _write(name: str, fields: list[str], rows: list[dict[str, object]]) -> None:
        with (mt5_dir / name).open("w", encoding="utf-8", newline="") as handle:
            writer = _csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    _write(
        f"mt5_risk_account_utc_{account}.csv",
        [
            "time_msc", "account_login", "server", "currency", "balance", "equity", "margin",
            "free_margin", "margin_level", "leverage", "open_positions", "trade_allowed",
            "terminal_connected",
        ],
        [{
            "time_msc": captured_msc, "account_login": account, "server": "Demo-Server",
            "currency": "USD", "balance": 10_000.0, "equity": 10_000.0, "margin": 0.0,
            "free_margin": 10_000.0, "margin_level": 0, "leverage": 100, "open_positions": 0,
            "trade_allowed": 1, "terminal_connected": 1,
        }],
    )
    _write(f"mt5_risk_positions_utc_{account}.csv", ["time_msc", "account_login"], [])
    _write(f"mt5_risk_symbols_utc_{account}.csv", ["time_msc", "account_login"], [])


def _candidate_payload(signal_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "signal_id": signal_id,
        "observed_at": now,
        "created_at": now,
        "symbol": _SYMBOL,
        "timeframe": "M5",
        "setup_family": "spread_pressure",
        "scenario": "continuation",
        "plan": {
            "action": "BUY",
            "entries": [{"price": 2000.0, "allocation": 1.0, "rationale": "test", "order_type": "MARKET"}],
            "stop_price": 1990.0,
            "targets": [2020.0],
            "invalidation": "close below stop",
            "target_rationale": ["r1"],
        },
        "market_features": {}, "factor_scores": {}, "factor_reasons": {},
        "provenance": ["test"], "generated_from_market_data": True, "robot_context_only": {},
    }


def _write_journal(path: Path, *, signal_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_candidate_payload(signal_id), sort_keys=True) + "\n", encoding="utf-8")


def _discover(tmp_path: Path, **overrides: object) -> pipeline_module.DiscoveredInputs:
    kwargs: dict[str, object] = dict(
        data_root=tmp_path,
        runtime_root=None,
        db_path=None,
        candidates_path=None,
        mt5_export_dir=None,
        login=_MT5_ACCOUNT,
        risk_profile_path=None,
        correlations_path=None,
        repo_root=REPO_ROOT,
    )
    kwargs.update(overrides)
    return pipeline_module.discover_inputs(**kwargs)


def test_default_candidate_journal_is_runtime_root_candidates_jsonl(tmp_path: Path) -> None:
    _write_journal(tmp_path / "live_signal_runtime_v1" / "candidates.jsonl", signal_id="live-1")
    _write_mt5_exports(tmp_path / "mt5")
    inputs = _discover(tmp_path)
    assert inputs.runtime_root == tmp_path / "live_signal_runtime_v1"
    assert inputs.candidates_path == tmp_path / "live_signal_runtime_v1" / "candidates.jsonl"


def test_historical_research_journal_is_not_silently_used_as_live_default(tmp_path: Path) -> None:
    # ONLY the historical/research archive exists -- no live journal at all.
    _write_journal(tmp_path / "signal_intelligence_v1_16" / "candidates.jsonl", signal_id="historical-1")
    _write_mt5_exports(tmp_path / "mt5")
    try:
        _discover(tmp_path)
    except pipeline_module.PipelineGapError as exc:
        assert "live" in str(exc).lower()
        assert "signal_intelligence_v1_16" not in str(exc)
    else:
        raise AssertionError(
            "discover_inputs must fail closed, not silently fall back to the "
            "historical signal_intelligence_v1_16 journal"
        )


def test_default_selects_live_journal_even_when_historical_journal_also_present(tmp_path: Path) -> None:
    # Both a historical archive AND a live journal exist -- the default must
    # select the live one, proving the historical path is never preferred.
    historical_path = tmp_path / "signal_intelligence_v1_16" / "candidates.jsonl"
    live_path = tmp_path / "live_signal_runtime_v1" / "candidates.jsonl"
    _write_journal(historical_path, signal_id="historical-1")
    _write_journal(live_path, signal_id="live-1")
    _write_mt5_exports(tmp_path / "mt5")
    inputs = _discover(tmp_path)
    assert inputs.candidates_path == live_path
    assert inputs.candidates_path != historical_path
    candidates = pipeline_module.load_candidates(inputs.candidates_path)
    assert len(candidates) == 1  # loaded from the live journal, not both/merged.


def test_explicit_runtime_root_override_selects_an_account_specific_workspace(tmp_path: Path) -> None:
    # Requirement 11: an ECN market-data account's own isolated runtime
    # root, distinct from the shared default -- selected explicitly, never
    # silently.
    ecn_root = tmp_path / "live_signal_runtime_ecN_77053345"
    _write_journal(ecn_root / "candidates.jsonl", signal_id="ecn-1")
    _write_mt5_exports(tmp_path / "mt5")
    inputs = _discover(tmp_path, runtime_root=ecn_root)
    assert inputs.runtime_root == ecn_root
    assert inputs.candidates_path == ecn_root / "candidates.jsonl"


def test_explicit_candidates_override_still_works_regardless_of_runtime_root(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere" / "candidates.jsonl"
    _write_journal(explicit, signal_id="explicit-1")
    _write_mt5_exports(tmp_path / "mt5")
    inputs = _discover(tmp_path, candidates_path=explicit)
    assert inputs.candidates_path == explicit


def test_cli_parser_exposes_runtime_root_flag_defaulting_to_none() -> None:
    parser = pipeline_module.build_arg_parser()
    args = parser.parse_args([
        "--hypothesis-id", "x", "--account", _MT5_ACCOUNT,
        "--demo-account-allowlist", _MT5_ACCOUNT,
    ])
    assert args.runtime_root is None


# ---------------------------------------------------------------------------
# 9: incremental v1.22.1 watermark logic untouched by this task.
# ---------------------------------------------------------------------------


def test_v122_runtime_still_reuses_v121_watermark_primitives() -> None:
    text = LIVE_RUNTIME_V122_MODULE.read_text(encoding="utf-8")
    assert "from trademind.live_signal_runtime import" in text
    assert "latest_closed_watermarks" in text


def test_v121_module_still_defines_the_watermark_primitives() -> None:
    text = LIVE_RUNTIME_MODULE.read_text(encoding="utf-8")
    assert "def latest_closed_watermarks" in text
    assert "candidates_path = runtime_root / \"candidates.jsonl\"" in text


# ---------------------------------------------------------------------------
# 10: signal freshness stays at 900 seconds in both shipped risk profiles.
# ---------------------------------------------------------------------------


def test_signal_freshness_900_seconds_preserved_in_both_profiles() -> None:
    for path in (STANDARD_PROFILE, SUPERVISED_PROFILE):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["maximum_signal_age_seconds"] == 900, path


# ---------------------------------------------------------------------------
# 11: no broker order-send machinery anywhere in the live ingest chain.
# ---------------------------------------------------------------------------


def test_no_broker_order_machinery_in_live_ingest_scripts_and_modules() -> None:
    forbidden = re.compile(r"order_send|OrderSend\(|trade\.Buy\(|trade\.Sell\(|CTrade|MetaTrader5\.", re.I)
    for path in (
        WATCH_SCRIPT, RUNTIME_SCRIPT, INSTALL_SCRIPT,
        LIVE_RUNTIME_MODULE, LIVE_RUNTIME_V122_MODULE,
    ):
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"broker order machinery found in {path}"


def test_v122_module_still_documents_read_only_orders_off_publication_off() -> None:
    text = LIVE_RUNTIME_V122_MODULE.read_text(encoding="utf-8")
    assert '"orders_enabled": False' in text
    assert '"signal_publication_enabled": False' in text


# ---------------------------------------------------------------------------
# 12: market-data account (77053345) vs demo-execution account (67206924)
# separation preserved and documented, never silently changed.
# ---------------------------------------------------------------------------


def test_market_data_and_execution_account_separation_is_documented() -> None:
    doc_text = V121_DOC.read_text(encoding="utf-8")
    assert "77053345" in doc_text
    assert "67206924" in doc_text
    assert "-RuntimeRoot" in doc_text


def test_no_script_touched_by_this_task_hardcodes_account_77053345_as_execution_login() -> None:
    # The market-data account must never silently become the demo execution
    # login used for order placement -- neither watch/runtime/install
    # scripts nor the SER8 pipeline itself ships a hardcoded "77053345".
    for path in (WATCH_SCRIPT, RUNTIME_SCRIPT, INSTALL_SCRIPT):
        assert "77053345" not in path.read_text(encoding="utf-8")
    pipeline_text = (REPO_ROOT / "scripts" / "run_ser8_real_demo_pipeline.py").read_text(encoding="utf-8")
    assert "77053345" not in pipeline_text


# ---------------------------------------------------------------------------
# 13: nothing was silently deleted/removed under requirement 12 without
# proof of supersession -- the standalone console dashboard remains, since
# it reads an entirely unrelated data source and is not a competing SER8
# candidate-generation entrypoint.
# ---------------------------------------------------------------------------


def test_unrelated_console_dashboard_script_is_not_a_competing_candidate_entrypoint() -> None:
    console_script = REPO_ROOT / "scripts" / "run_v112_live_signal_console.ps1"
    assert console_script.is_file()
    text = console_script.read_text(encoding="utf-8")
    assert "schtasks.exe /Create" not in text
    assert "candidates.jsonl" not in text
