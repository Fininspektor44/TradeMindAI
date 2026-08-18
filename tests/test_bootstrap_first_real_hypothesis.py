"""Tests for scripts/bootstrap_first_real_hypothesis.py."""

from __future__ import annotations

import ast
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

bootstrap_module = importlib.import_module("bootstrap_first_real_hypothesis")

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState  # noqa: E402

_SYMBOL = "XAUUSD"
_TIMEFRAME = "M5"
_SETUP_FAMILY = "spread_pressure"
_ACTION = "BUY"
_METRIC = "avg_net_atr"


def _write_candidate_journal(data_root: Path, *, symbol: str = _SYMBOL, count: int = 1) -> None:
    candidates_dir = data_root / "signal_intelligence_v1_16"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    lines = []
    for i in range(count):
        payload = {
            "signal_id": f"sig-{i}",
            "observed_at": now,
            "created_at": now,
            "symbol": symbol,
            "timeframe": _TIMEFRAME,
            "setup_family": _SETUP_FAMILY,
            "scenario": "continuation",
            "plan": {
                "action": _ACTION,
                "entries": [{"price": 2000.0, "allocation": 1.0, "rationale": "test", "order_type": "MARKET"}],
                "stop_price": 1990.0,
                "targets": [2020.0],
                "invalidation": "close below stop",
                "target_rationale": ["r1"],
            },
            "market_features": {},
            "factor_scores": {},
            "factor_reasons": {},
            "provenance": ["test"],
            "generated_from_market_data": True,
            "robot_context_only": {},
        }
        lines.append(json.dumps(payload, sort_keys=True))
    (candidates_dir / "candidates.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_source_csv(path: Path, *, rows: int = 20) -> None:
    lines = [f"time,{_METRIC}"]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(rows):
        lines.append(f"{(start + timedelta(hours=i)).isoformat()},{10 + i}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _full_argv(tmp_path: Path, data_root: Path, source_csv: Path, **overrides) -> list[str]:
    values = {
        "--data-root": str(data_root),
        "--symbol": _SYMBOL,
        "--timeframe": _TIMEFRAME,
        "--setup-family": _SETUP_FAMILY,
        "--action-scope": _ACTION,
        "--horizon": "3",
        "--test-family": "deterministic_aggregate_v1",
        "--primary-metric": _METRIC,
        "--criterion-threshold": "0.0",
        "--final-holdout-metric": "row_count",
        "--final-holdout-threshold": "1",
        "--alpha": "0.05",
        "--q": "0.10",
        "--minimum-effect-size": "0.0",
        "--max-hypotheses-tests": "1",
        "--research-source-csv": str(source_csv),
        "--title": "Regime-conditioned continuation",
        "--rationale": "The candidate may depend on volatility regime.",
        "--falsifiable-claim": "The effect remains positive in a predefined regime.",
        "--proposed-test": "Compare predefined public-data subsets for the regime.",
        "--rejection-condition": "Reject if the regime effect is non-positive.",
        "--confidence": "HIGH",
        "--reviewer-id": "operator:test-reviewer",
        "--created-by": "operator:test-creator",
    }
    values.update(overrides)
    argv: list[str] = []
    for key, value in values.items():
        argv.extend([key, value])
    return argv


@pytest.fixture()
def real_root(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    _write_candidate_journal(data_root)
    source_csv = tmp_path / "source.csv"
    _write_source_csv(source_csv)
    return data_root, source_csv


# ---------------------------------------------------------------------------
# 1. Review mode writes zero registry state.
# ---------------------------------------------------------------------------


def test_review_mode_writes_zero_registry_state(real_root, capsys) -> None:
    data_root, source_csv = real_root
    argv = _full_argv(None, data_root, source_csv)
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 0
    assert not (data_root / "ser8_registry.db").exists()
    assert not (data_root / "ser8_artifacts").exists()
    captured = capsys.readouterr()
    assert "REVIEW (NOTHING WRITTEN TO REGISTRY)" in captured.out
    assert "hypothesis_id" in captured.out


def test_review_and_approve_produce_identical_hypothesis_id(real_root, capsys) -> None:
    data_root, source_csv = real_root
    argv = _full_argv(None, data_root, source_csv)
    bootstrap_module.main(argv)
    review_out = capsys.readouterr().out
    review_id = next(line for line in review_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    bootstrap_module.main(argv + ["--approve"])
    approve_out = capsys.readouterr().out
    approve_id = next(line for line in approve_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    assert review_id == approve_id


# ---------------------------------------------------------------------------
# 2. Approve required to create anything.
# ---------------------------------------------------------------------------


def test_approve_required_for_real_creation(real_root) -> None:
    data_root, source_csv = real_root
    argv = _full_argv(None, data_root, source_csv)
    bootstrap_module.main(argv)  # review only
    assert not (data_root / "ser8_registry.db").exists()
    bootstrap_module.main(argv + ["--approve"])
    assert (data_root / "ser8_registry.db").exists()


# ---------------------------------------------------------------------------
# 3. No invented research parameters.
# ---------------------------------------------------------------------------


def test_missing_required_parameters_reported_together(real_root, capsys) -> None:
    data_root, source_csv = real_root
    exit_code = bootstrap_module.main(["--data-root", str(data_root)])
    assert exit_code == 2
    captured = capsys.readouterr()
    for flag in (
        "--symbol", "--timeframe", "--setup-family", "--action-scope", "--horizon",
        "--test-family", "--primary-metric", "--criterion-threshold",
        "--final-holdout-metric", "--final-holdout-threshold", "--alpha", "--q",
        "--minimum-effect-size", "--max-hypotheses-tests", "--research-source-csv",
        "--title", "--rationale", "--falsifiable-claim", "--proposed-test",
        "--rejection-condition", "--confidence", "--reviewer-id", "--created-by",
    ):
        assert flag in captured.err
    assert "Fill in and re-run" in captured.err
    assert "--approve" in captured.err


def test_no_hidden_default_for_any_research_parameter() -> None:
    parser = bootstrap_module.build_arg_parser()
    for action in parser._actions:
        if action.dest in bootstrap_module._RESEARCH_PARAMETER_ARGS:
            assert action.default is None, f"{action.dest} must have no hidden default"


def test_no_hardcoded_statistical_constant_in_source() -> None:
    """The script must never hardcode a specific metric name, threshold,
    alpha/q/effect-size value, or symbol/timeframe -- every one of those
    must flow only from argparse."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("finalize_success", "create_specification"):
                for keyword in node.keywords:
                    if keyword.arg in ("alpha", "q", "minimum_effect_size", "primary_metric", "test_family"):
                        assert isinstance(keyword.value, ast.Attribute), (
                            f"{keyword.arg} must come from args.*, not a literal"
                        )


# ---------------------------------------------------------------------------
# 4. Genuine journal loader only.
# ---------------------------------------------------------------------------


def test_unobserved_combination_reports_real_alternatives(real_root, capsys) -> None:
    data_root, source_csv = real_root
    argv = _full_argv(None, data_root, source_csv, **{"--symbol": "EURUSD"})
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "no journaled candidate matches" in captured.err
    assert _SYMBOL in captured.err  # the real, actually-observed symbol is listed


def test_empty_candidate_journal_reports_gap(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    (data_root / "signal_intelligence_v1_16").mkdir(parents=True)
    (data_root / "signal_intelligence_v1_16" / "candidates.jsonl").write_text("", encoding="utf-8")
    source_csv = tmp_path / "source.csv"
    _write_source_csv(source_csv)
    argv = _full_argv(None, data_root, source_csv)
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 2


def test_never_constructs_signal_candidate_directly_in_source() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    assert "SignalCandidate(" not in source


# ---------------------------------------------------------------------------
# 5. Authoritative intake/spec/manifest path only; retired lifecycle unreachable.
# ---------------------------------------------------------------------------


def test_script_never_imports_retired_lineage() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    forbidden = {
        "trademind.final_holdout_evaluation",
        "trademind.experiment_execution_runtime",
        "trademind.experiment_evidence",
        "trademind.validation_decision",
        "trademind.smc_pattern_journal_evaluator",
    }
    assert not (imported & forbidden), imported & forbidden
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "evaluate":
            pytest.fail("script must never call .evaluate() (the retired entry point's name)")


def test_script_never_calls_train_test_or_validation_or_holdout_controls() -> None:
    """This bootstrap only ever reaches FROZEN -- it must never import or
    construct any of the TRAIN_TESTED/VALIDATION/HOLDOUT/terminal
    authoritative controls."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    for forbidden_name in (
        "TrainTestExecutionControl",
        "ValidationExecutionControl",
        "HoldoutTriggerBridge",
        "FinalVerdictAcceptanceControl",
        "FinalHoldoutRunner",
        "FinalHoldoutSealer",
    ):
        assert forbidden_name not in source


# ---------------------------------------------------------------------------
# 6. Resulting state exactly FROZEN.
# ---------------------------------------------------------------------------


def test_approve_reaches_exactly_frozen(real_root, capsys) -> None:
    data_root, source_csv = real_root
    argv = _full_argv(None, data_root, source_csv)
    bootstrap_module.main(argv)
    capsys.readouterr()
    exit_code = bootstrap_module.main(argv + ["--approve"])
    assert exit_code == 0
    captured = capsys.readouterr()
    hypothesis_id = next(line for line in captured.out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    record = registry.get(hypothesis_id)
    assert record.state is HypothesisState.FROZEN

    assert "run_ser8_real_demo_pipeline.py" in captured.out
    assert "--account 67206924" in captured.out
    assert hypothesis_id in captured.out


def test_no_broker_order_sent_by_bootstrap() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    forbidden = ("SER8DemoOrderSendControl", "FileBridgeDemoOrderTransport", "MetaTrader5", "order_send", "OrderSend")
    for term in forbidden:
        assert term not in source
