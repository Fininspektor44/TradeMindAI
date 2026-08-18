"""Tests for scripts/bootstrap_first_real_hypothesis.py."""

from __future__ import annotations

import ast
import csv
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
from trademind.signal_intelligence import candidate_from_dict  # noqa: E402

_SYMBOL = "XAUUSD"
_TIMEFRAME = "M5"
_SETUP_FAMILY = "spread_pressure"
_ACTION = "BUY"
_METRIC = "net_move"

_PAPER_FIELDS = (
    "paper_signal_id", "generated_at", "rule_id", "tier", "source_signal_id", "signal_time",
    "symbol", "timeframe", "action", "label", "horizon", "entry_price", "score", "confidence",
    "spread_points", "spread_cost_atr", "training_status", "training_trades", "training_days",
    "training_win_rate", "training_pf_atr", "training_avg_net_atr", "training_early_avg_net_atr",
    "training_late_avg_net_atr", "training_max_drawdown_atr", "training_max_loss_streak",
    "training_ci_low", "training_ci_high", "training_p_value", "training_q_value", "exit_time",
    "net_move", "progress_atr", "mfe_atr", "mae_atr", "outcome",
)

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candidate_payload(*, index: int, symbol: str = _SYMBOL, timeframe: str = _TIMEFRAME,
                        setup_family: str = _SETUP_FAMILY, action: str = _ACTION) -> dict:
    t = (_START + timedelta(hours=index)).isoformat()
    if action == "SELL":
        stop_price, targets, invalidation = 2010.0, [1980.0], "close above stop"
    else:
        stop_price, targets, invalidation = 1990.0, [2020.0], "close below stop"
    return {
        "observed_at": t, "created_at": t, "symbol": symbol, "timeframe": timeframe,
        "setup_family": setup_family, "scenario": "continuation",
        "plan": {
            "action": action,
            "entries": [{"price": 2000.0, "allocation": 1.0, "rationale": "test", "order_type": "MARKET"}],
            "stop_price": stop_price, "targets": targets, "invalidation": invalidation,
            "target_rationale": ["r1"],
        },
        "market_features": {}, "factor_scores": {}, "factor_reasons": {}, "provenance": ["test"],
        "generated_from_market_data": True, "robot_context_only": {},
    }


def _real_signal_id(payload: dict) -> str:
    """The genuine, deterministically-computed SignalCandidate.signal_id --
    never a value hand-typed into the payload's own (ignored) 'signal_id'
    key."""
    return candidate_from_dict(payload).signal_id


def _write_candidate_journal(data_root: Path, payloads: list[dict]) -> None:
    candidates_dir = data_root / "signal_intelligence_v1_16"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(payload, sort_keys=True) for payload in payloads]
    (candidates_dir / "candidates.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _paper_row(*, source_signal_id: str, index: int, symbol: str = _SYMBOL, timeframe: str = _TIMEFRAME,
               action: str = _ACTION, net_move: float | None = None) -> dict:
    t = (_START + timedelta(hours=index)).isoformat()
    row = {field: "" for field in _PAPER_FIELDS}
    row.update({
        "paper_signal_id": f"{source_signal_id}|r1|H1", "generated_at": t, "rule_id": "r1", "tier": "A",
        "source_signal_id": source_signal_id, "signal_time": t, "symbol": symbol, "timeframe": timeframe,
        "action": action, "label": "X", "horizon": "1", "entry_price": "2000", "score": "1",
        "confidence": "1", "spread_points": "1", "spread_cost_atr": "0.01", "training_status": "OK",
        "training_trades": "10", "training_days": "5", "training_win_rate": "0.5",
        "training_pf_atr": "1.2", "training_avg_net_atr": "0.1", "training_early_avg_net_atr": "0.1",
        "training_late_avg_net_atr": "0.1", "training_max_drawdown_atr": "0.1",
        "training_max_loss_streak": "1", "training_ci_low": "0", "training_ci_high": "0.2",
        "training_p_value": "0.05", "training_q_value": "0.05", "exit_time": t,
        "net_move": str(1.0 + index if net_move is None else net_move),
        "progress_atr": "0.5", "mfe_atr": str(2.0 + index), "mae_atr": str(-1.0 - index * 0.1),
        "outcome": "WIN",
    })
    return row


def _write_paper_signals(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_PAPER_FIELDS))
        writer.writeheader()
        writer.writerows(rows)


def _full_argv(data_root: Path, **overrides) -> list[str]:
    values = {
        "--data-root": str(data_root),
        "--symbol": _SYMBOL, "--timeframe": _TIMEFRAME, "--setup-family": _SETUP_FAMILY,
        "--action-scope": _ACTION, "--horizon": "3",
        "--test-family": "deterministic_aggregate_v1", "--primary-metric": _METRIC,
        "--criterion-threshold": "0.0", "--final-holdout-metric": "row_count",
        "--final-holdout-threshold": "1", "--alpha": "0.05", "--q": "0.10",
        "--minimum-effect-size": "0.0", "--max-hypotheses-tests": "1",
        "--title": "Regime-conditioned continuation",
        "--rationale": "The candidate may depend on volatility regime.",
        "--falsifiable-claim": "The effect remains positive in a predefined regime.",
        "--proposed-test": "Compare predefined public-data subsets for the regime.",
        "--rejection-condition": "Reject if the regime effect is non-positive.",
        "--confidence": "HIGH", "--reviewer-id": "operator:test-reviewer",
        "--created-by": "operator:test-creator",
    }
    values.update(overrides)
    argv: list[str] = []
    for key, value in values.items():
        argv.extend([key, value])
    return argv


@pytest.fixture()
def genuine_scope(tmp_path: Path) -> Path:
    """A real candidate journal (8 in-scope candidates) genuinely joined to
    a real paper-signals journal (one matching outcome row per candidate),
    using the SAME real, deterministically-computed signal_id both files
    must share to join at all."""
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(8)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    rows = [_paper_row(source_signal_id=sid, index=i) for i, sid in enumerate(real_ids)]
    _write_paper_signals(data_root / "paper_signals" / "signals.csv", rows)
    return data_root


# ---------------------------------------------------------------------------
# 1. Unrelated CSV cannot be used.
# ---------------------------------------------------------------------------


def test_no_research_source_csv_flag_exists() -> None:
    parser = bootstrap_module.build_arg_parser()
    flags = {action.option_strings[0] for action in parser._actions if action.option_strings}
    assert "--research-source-csv" not in flags


def test_no_arbitrary_path_argument_reaches_dataset_artifact_construction() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    assert "args.research_source_csv" not in source
    assert "research_source_csv" not in source


# ---------------------------------------------------------------------------
# 2. Wrong symbol/timeframe/setup/action cannot leak rows; genuine join only.
# ---------------------------------------------------------------------------


def test_wrong_symbol_paper_row_cannot_leak_in(genuine_scope: Path) -> None:
    data_root = genuine_scope
    candidates_path = data_root / "signal_intelligence_v1_16" / "candidates.jsonl"
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(candidates_path, key)
    paper_rows = bootstrap_module.load_paper_signal_rows(data_root / "paper_signals" / "signals.csv")
    # Inject one paper row whose source_signal_id happens to collide with a
    # real candidate's signal_id but whose OWN symbol disagrees.
    tampered = list(paper_rows)
    victim_id = next(iter(candidates))
    tampered.append(_paper_row(source_signal_id=victim_id, index=99, symbol="EURUSD", net_move=999.0))
    bound = bootstrap_module.bind_dataset_to_candidate_scope(candidates, tampered, key=key)
    assert "999.0" not in bound.csv_bytes.decode("utf-8")
    assert bound.matched_outcome_rows == 8  # the tampered row is rejected, not counted


def test_unmatched_candidate_not_in_dataset(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(5)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    # Only 5 rows so 4 of 5 candidates match (below the minimum), plus one
    # extra unrelated candidate outside scope entirely (different symbol).
    rows = [_paper_row(source_signal_id=sid, index=i) for i, sid in enumerate(real_ids[:4])]
    _write_paper_signals(data_root / "paper_signals" / "signals.csv", rows)
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    paper_rows = bootstrap_module.load_paper_signal_rows(data_root / "paper_signals" / "signals.csv")
    # 4 matched candidates, below MINIMUM_DATASET_ROWS -- must fail closed,
    # never silently proceed with an undersized dataset.
    with pytest.raises(bootstrap_module.BootstrapGapError):
        bootstrap_module.bind_dataset_to_candidate_scope(candidates, paper_rows, key=key)


def test_join_uses_only_signal_id_source_signal_id(genuine_scope: Path) -> None:
    """A paper row that matches by symbol/timeframe/action but whose
    source_signal_id does not correspond to ANY real candidate must never
    be pulled in."""
    data_root = genuine_scope
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    paper_rows = bootstrap_module.load_paper_signal_rows(data_root / "paper_signals" / "signals.csv")
    paper_rows = list(paper_rows) + [_paper_row(source_signal_id="TM-NOT-A-REAL-CANDIDATE", index=50, net_move=12345.0)]
    bound = bootstrap_module.bind_dataset_to_candidate_scope(candidates, paper_rows, key=key)
    assert "12345.0" not in bound.csv_bytes.decode("utf-8")
    assert bound.matched_candidates == 8


# ---------------------------------------------------------------------------
# 3. Canonical time derived from real signal_time.
# ---------------------------------------------------------------------------


def test_canonical_time_derived_from_real_signal_time(genuine_scope: Path) -> None:
    data_root = genuine_scope
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    paper_rows = bootstrap_module.load_paper_signal_rows(data_root / "paper_signals" / "signals.csv")
    bound = bootstrap_module.bind_dataset_to_candidate_scope(candidates, paper_rows, key=key)
    reader = csv.DictReader(bound.csv_bytes.decode("utf-8").splitlines())
    rows = list(reader)
    assert "time" in reader.fieldnames
    for row in rows:
        # Every printed time must equal a real signal_time from the fixture.
        parsed = datetime.fromisoformat(row["time"])
        assert _START <= parsed <= _START + timedelta(hours=7)
    times = [row["time"] for row in rows]
    assert times == sorted(times)


def test_invalid_signal_time_fails_closed(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(5)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    rows = [_paper_row(source_signal_id=sid, index=i) for i, sid in enumerate(real_ids)]
    rows[0]["signal_time"] = "not-a-real-timestamp"
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    with pytest.raises(bootstrap_module.BootstrapGapError):
        bootstrap_module.bind_dataset_to_candidate_scope(candidates, rows, key=key)


# ---------------------------------------------------------------------------
# 4. Deterministic derived dataset hash.
# ---------------------------------------------------------------------------


def test_dataset_hash_is_deterministic(genuine_scope: Path) -> None:
    data_root = genuine_scope
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates_path = data_root / "signal_intelligence_v1_16" / "candidates.jsonl"
    paper_path = data_root / "paper_signals" / "signals.csv"
    candidates_1 = bootstrap_module.select_candidates_by_scope(candidates_path, key)
    rows_1 = bootstrap_module.load_paper_signal_rows(paper_path)
    bound_1 = bootstrap_module.bind_dataset_to_candidate_scope(candidates_1, rows_1, key=key)

    candidates_2 = bootstrap_module.select_candidates_by_scope(candidates_path, key)
    rows_2 = bootstrap_module.load_paper_signal_rows(paper_path)
    bound_2 = bootstrap_module.bind_dataset_to_candidate_scope(candidates_2, rows_2, key=key)

    assert bound_1.dataset_hash == bound_2.dataset_hash
    assert bound_1.csv_bytes == bound_2.csv_bytes


def test_dataset_hash_changes_with_different_scope(genuine_scope: Path) -> None:
    data_root = genuine_scope
    # Add a second, disjoint scope (SELL instead of BUY) with its own real
    # candidates/paper rows so the two derived datasets must hash differently.
    payloads_sell = [_candidate_payload(index=i, action="SELL") for i in range(5)]
    existing = json.loads(
        "[" + ",".join((data_root / "signal_intelligence_v1_16" / "candidates.jsonl").read_text().strip().splitlines()) + "]"
    )
    all_payloads = existing + payloads_sell
    _write_candidate_journal(data_root, all_payloads)
    real_ids_sell = [_real_signal_id(p) for p in payloads_sell]
    existing_rows = bootstrap_module.load_paper_signal_rows(data_root / "paper_signals" / "signals.csv")
    new_rows = [_paper_row(source_signal_id=sid, index=i, action="SELL") for i, sid in enumerate(real_ids_sell)]
    _write_paper_signals(data_root / "paper_signals" / "signals.csv", list(existing_rows) + new_rows)

    key_buy = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope="BUY"
    )
    key_sell = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope="SELL"
    )
    candidates_path = data_root / "signal_intelligence_v1_16" / "candidates.jsonl"
    paper_path = data_root / "paper_signals" / "signals.csv"
    bound_buy = bootstrap_module.bind_dataset_to_candidate_scope(
        bootstrap_module.select_candidates_by_scope(candidates_path, key_buy),
        bootstrap_module.load_paper_signal_rows(paper_path), key=key_buy,
    )
    # Exactly 5 SELL candidates -- meets MINIMUM_DATASET_ROWS, so this must
    # succeed and produce a dataset hash that differs from the BUY scope's.
    bound_sell = bootstrap_module.bind_dataset_to_candidate_scope(
        bootstrap_module.select_candidates_by_scope(candidates_path, key_sell),
        bootstrap_module.load_paper_signal_rows(paper_path), key=key_sell,
    )
    assert bound_buy.dataset_hash != bound_sell.dataset_hash


# ---------------------------------------------------------------------------
# 5. Review writes no real state.
# ---------------------------------------------------------------------------


def test_review_mode_writes_zero_registry_state(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 0
    assert not (data_root / "ser8_registry.db").exists()
    assert not (data_root / "ser8_artifacts").exists()
    assert not (data_root / "ser8_bootstrap_datasets").exists()
    captured = capsys.readouterr()
    assert "MATCHED CANDIDATES: 8" in captured.out
    assert "MATCHED OUTCOME ROWS: 8" in captured.out
    assert "UNMATCHED CANDIDATES: 0" in captured.out
    assert "DATASET HASH: sha256:" in captured.out
    assert "AVAILABLE NUMERIC METRICS:" in captured.out
    assert "CANDIDATE SCOPE:" in captured.out


def test_review_and_approve_produce_identical_hypothesis_and_dataset_hash(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv)
    review_out = capsys.readouterr().out
    review_id = next(line for line in review_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()
    review_hash = next(line for line in review_out.splitlines() if line.startswith("DATASET HASH:")).split(":", 1)[1].strip()

    bootstrap_module.main(argv + ["--approve"])
    approve_out = capsys.readouterr().out
    approve_id = next(line for line in approve_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()
    approve_hash_line = next(line for line in approve_out.splitlines() if "dataset_hash" in line)

    assert review_id == approve_id
    assert review_hash in approve_hash_line
    assert (data_root / "ser8_bootstrap_datasets").is_dir()

    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    assert registry.get(approve_id).state is HypothesisState.FROZEN


def test_primary_metric_must_be_a_genuinely_available_metric(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root, **{"--primary-metric": "totally_invented_metric"})
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "not one of the genuinely available numeric metrics" in captured.err


def test_zero_matched_rows_fails_closed(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(6)]
    _write_candidate_journal(data_root, payloads)
    # Paper journal has rows, but for a completely different, unrelated set
    # of source_signal_id values -- zero genuine matches.
    rows = [_paper_row(source_signal_id="TM-UNRELATED-0000", index=i) for i in range(6)]
    _write_paper_signals(data_root / "paper_signals" / "signals.csv", rows)
    argv = _full_argv(data_root)
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "zero paper-signal rows matched" in captured.err
    assert not (data_root / "ser8_registry.db").exists()


# ---------------------------------------------------------------------------
# 6. AST/source-scan invariants (authoritative path, no synthetic rows).
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


def test_script_never_constructs_train_test_or_holdout_controls() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    for forbidden_name in (
        "TrainTestExecutionControl", "ValidationExecutionControl", "HoldoutTriggerBridge",
        "FinalVerdictAcceptanceControl", "FinalHoldoutRunner", "FinalHoldoutSealer",
    ):
        assert forbidden_name not in source


def test_no_broker_order_sent_by_bootstrap() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    for term in ("SER8DemoOrderSendControl", "FileBridgeDemoOrderTransport", "MetaTrader5", "order_send", "OrderSend"):
        assert term not in source


def test_no_hidden_default_for_any_research_parameter() -> None:
    parser = bootstrap_module.build_arg_parser()
    for action in parser._actions:
        if action.dest in bootstrap_module._RESEARCH_PARAMETER_ARGS:
            assert action.default is None, f"{action.dest} must have no hidden default"


def test_no_synthetic_row_construction_in_bind_function() -> None:
    """bind_dataset_to_candidate_scope must only ever COPY an existing
    paper_rows entry; it must never construct a numeric metric value from a
    hardcoded literal (an empty-dict/list initializer is fine)."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "bind_dataset_to_candidate_scope"
    )
    for node in ast.walk(function):
        if isinstance(node, ast.Dict) and node.values:
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
                    pytest.fail(
                        "bind_dataset_to_candidate_scope must never hardcode a numeric dict value"
                    )
