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
from trademind.signal_intelligence import candidate_from_dict  # noqa: E402

_SYMBOL = "EURUSD"
_TIMEFRAME = "M5"
_SETUP_FAMILY = "MULTIFACTOR_MARKET_SETUP"
_ACTION = "BUY"
_METRIC = "net_r"

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candidate_payload(*, index: int, symbol: str = _SYMBOL, timeframe: str = _TIMEFRAME,
                        setup_family: str = _SETUP_FAMILY, action: str = _ACTION) -> dict:
    t = (_START + timedelta(hours=index)).isoformat()
    if action == "SELL":
        stop_price, targets, invalidation = 1.12, [1.08], "close above stop"
    else:
        stop_price, targets, invalidation = 1.08, [1.12], "close below stop"
    return {
        "observed_at": t, "created_at": t, "symbol": symbol, "timeframe": timeframe,
        "setup_family": setup_family, "scenario": "continuation",
        "plan": {
            "action": action,
            "entries": [{"price": 1.10, "allocation": 1.0, "rationale": "test", "order_type": "MARKET"}],
            "stop_price": stop_price, "targets": targets, "invalidation": invalidation,
            "target_rationale": ["r1"],
        },
        "market_features": {}, "factor_scores": {}, "factor_reasons": {}, "provenance": ["test"],
        "generated_from_market_data": True, "robot_context_only": {},
    }


def _real_signal_id(payload: dict) -> str:
    return candidate_from_dict(payload).signal_id


def _write_candidate_journal(data_root: Path, payloads: list[dict]) -> None:
    candidates_dir = data_root / "signal_intelligence_v1_16"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(payload, sort_keys=True) for payload in payloads]
    (candidates_dir / "candidates.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _outcome_payload(*, signal_id: str, index: int, outcome: str = "WIN", net_r: float | None = None) -> dict:
    completed_at = (_START + timedelta(hours=index, minutes=30)).isoformat()
    return {
        "schema_version": "shadow-v1",
        "signal_id": signal_id,
        "setup_key": "sk-test",
        "completed_at": completed_at,
        "outcome": outcome,
        "net_r": (1.0 + index if net_r is None else net_r) if outcome != "LOSS" else -(1.0 + index),
        "exit_reason": "TARGET",
        "exit_price": 1.12,
        "filled_entries": 1,
        "allocation_filled": 1.0,
        "average_entry": 1.10,
        "mfe_r": 2.0 + index,
        "mae_r": -0.5 - index * 0.1,
        "bars_observed": 10,
    }


def _write_outcomes_journal(data_root: Path, payloads: list[dict]) -> None:
    outcomes_dir = data_root / "signal_intelligence_v1_16"
    outcomes_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(payload, sort_keys=True) for payload in payloads]
    (outcomes_dir / "outcomes.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    a real canonical outcome journal (one matching outcome per candidate),
    keyed by the SAME real, deterministically-computed signal_id both files
    must share to join at all."""
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(8)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    outcomes = [
        _outcome_payload(signal_id=sid, index=i, outcome="WIN" if i % 2 == 0 else "LOSS")
        for i, sid in enumerate(real_ids)
    ]
    _write_outcomes_journal(data_root, outcomes)
    return data_root


# ---------------------------------------------------------------------------
# 1. Canonical outcome journal only -- paper_signals never used, no quarantine.
# ---------------------------------------------------------------------------


def test_no_paper_signals_flag_or_reference_exists() -> None:
    parser = bootstrap_module.build_arg_parser()
    flags = {action.option_strings[0] for action in parser._actions if action.option_strings}
    assert "--paper-signals" not in flags
    assert "--research-source-csv" not in flags
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    docstring_line_numbers: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docstring_line_numbers.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    code_lines = [
        line for index, line in enumerate(lines, start=1)
        if index not in docstring_line_numbers and not line.strip().startswith("#")
    ]
    # Executable, non-comment/non-docstring code must never open a
    # paper_signals path or a quarantine/live_runtime_v1/eCN duplicate
    # outcome path (the prose explaining WHY, in comments/docstrings, is
    # allowed to name them -- the "negative assertion, not the thing
    # itself" pattern already established elsewhere in this codebase).
    code_without_prose = "\n".join(code_lines)
    for forbidden in ("paper_signals", "quarantine", "live_signal_runtime_v1", "live_signal_runtime_eCN"):
        assert forbidden not in code_without_prose, forbidden


def test_default_outcomes_path_is_the_canonical_journal() -> None:
    parser = bootstrap_module.build_arg_parser()
    args = parser.parse_args(["--data-root", "/tmp/somewhere"] + [])
    assert args.outcomes is None  # no hidden override; run() computes the canonical default itself
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    assert 'data_root / "signal_intelligence_v1_16" / "outcomes.jsonl"' in source


# ---------------------------------------------------------------------------
# 2. Audit requirement: reuse the existing authoritative schema/parser.
# ---------------------------------------------------------------------------


def test_reuses_outcome_observation_schema() -> None:
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    assert "from trademind.signal_evidence import OutcomeObservation" in source
    assert "OutcomeObservation.from_dict" in source


# ---------------------------------------------------------------------------
# 3. Filter candidates exactly by symbol/timeframe/setup_family/action.
# ---------------------------------------------------------------------------


def test_out_of_scope_candidate_never_joins(genuine_scope: Path) -> None:
    data_root = genuine_scope
    # Add one more candidate with a DIFFERENT setup_family and its own real
    # outcome -- it must never appear in the EURUSD/M5/MULTIFACTOR_MARKET_SETUP/BUY dataset.
    outlier_payload = _candidate_payload(index=50, setup_family="OTHER_SETUP")
    all_payloads = [
        json.loads(line)
        for line in (data_root / "signal_intelligence_v1_16" / "candidates.jsonl").read_text().strip().splitlines()
    ] + [outlier_payload]
    _write_candidate_journal(data_root, all_payloads)
    outlier_id = _real_signal_id(outlier_payload)
    existing_outcomes = [
        json.loads(line)
        for line in (data_root / "signal_intelligence_v1_16" / "outcomes.jsonl").read_text().strip().splitlines()
    ]
    _write_outcomes_journal(
        data_root, existing_outcomes + [_outcome_payload(signal_id=outlier_id, index=50, net_r=99999.0)]
    )

    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    assert outlier_id not in candidates
    records = bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")
    bound = bootstrap_module.bind_dataset_to_outcome_journal_scope(candidates, records, key=key)
    assert "99999.0" not in bound.csv_bytes.decode("utf-8")
    assert bound.scope_candidates == 8


# ---------------------------------------------------------------------------
# 4. Genuine signal_id join only; 5,10: 315/323-style partial match accepted.
# ---------------------------------------------------------------------------


def test_partial_match_accepted_and_reported(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(8)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    # Only 6 of 8 candidates get a genuine outcome -- 2 unmatched, never fabricated.
    outcomes = [_outcome_payload(signal_id=sid, index=i) for i, sid in enumerate(real_ids[:6])]
    _write_outcomes_journal(data_root, outcomes)
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    records = bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")
    bound = bootstrap_module.bind_dataset_to_outcome_journal_scope(candidates, records, key=key)
    assert bound.scope_candidates == 8
    assert bound.matched_outcomes == 6
    assert bound.unmatched_candidates == 2
    assert len(bound.unmatched_candidate_ids) == 2


def test_join_uses_only_signal_id_no_fuzzy_matching(genuine_scope: Path) -> None:
    data_root = genuine_scope
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    records = dict(
        bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")
    )
    # An outcome for a signal_id that matches NO real candidate must never
    # be pulled in, even though its other fields look plausible.
    records["TM-NOT-A-REAL-CANDIDATE"] = _outcome_payload(
        signal_id="TM-NOT-A-REAL-CANDIDATE", index=99, net_r=12345.0
    )
    bound = bootstrap_module.bind_dataset_to_outcome_journal_scope(candidates, records, key=key)
    assert "12345.0" not in bound.csv_bytes.decode("utf-8")
    assert bound.matched_outcomes == 8


# ---------------------------------------------------------------------------
# 6. Canonical time derived only from genuine persisted timestamps.
# ---------------------------------------------------------------------------


def test_canonical_time_derived_from_real_candidate_observed_at(genuine_scope: Path) -> None:
    import csv

    data_root = genuine_scope
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    records = bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")
    bound = bootstrap_module.bind_dataset_to_outcome_journal_scope(candidates, records, key=key)
    reader = csv.DictReader(bound.csv_bytes.decode("utf-8").splitlines())
    rows = list(reader)
    assert "time" in reader.fieldnames
    assert "completed_at" in reader.fieldnames
    for row in rows:
        parsed = datetime.fromisoformat(row["time"])
        assert _START <= parsed <= _START + timedelta(hours=7)
    times = [row["time"] for row in rows]
    assert times == sorted(times)


# ---------------------------------------------------------------------------
# 7. Deterministic dataset hash.
# ---------------------------------------------------------------------------


def test_dataset_hash_deterministic_across_runs(genuine_scope: Path) -> None:
    data_root = genuine_scope
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates_path = data_root / "signal_intelligence_v1_16" / "candidates.jsonl"
    outcomes_path = data_root / "signal_intelligence_v1_16" / "outcomes.jsonl"

    bound_1 = bootstrap_module.bind_dataset_to_outcome_journal_scope(
        bootstrap_module.select_candidates_by_scope(candidates_path, key),
        bootstrap_module.load_outcome_records(outcomes_path), key=key,
    )
    bound_2 = bootstrap_module.bind_dataset_to_outcome_journal_scope(
        bootstrap_module.select_candidates_by_scope(candidates_path, key),
        bootstrap_module.load_outcome_records(outcomes_path), key=key,
    )
    assert bound_1.dataset_hash == bound_2.dataset_hash
    assert bound_1.csv_bytes == bound_2.csv_bytes


# ---------------------------------------------------------------------------
# 8/9. No synthetic metrics/rows/timestamps/fills; fail closed on malformed/
# duplicate/conflicting outcome identity.
# ---------------------------------------------------------------------------


def test_conflicting_duplicate_outcome_fails_closed(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(6)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    outcomes = [_outcome_payload(signal_id=sid, index=i) for i, sid in enumerate(real_ids)]
    # Append a SECOND, CONFLICTING record for the same signal_id (different net_r).
    conflicting = dict(outcomes[0])
    conflicting["net_r"] = 999.0
    outcomes.append(conflicting)
    _write_outcomes_journal(data_root, outcomes)
    with pytest.raises(bootstrap_module.BootstrapGapError, match="conflicting outcome identity"):
        bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")


def test_identical_duplicate_outcome_line_is_tolerated_and_counted(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(6)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    outcomes = [_outcome_payload(signal_id=sid, index=i) for i, sid in enumerate(real_ids)]
    outcomes.append(dict(outcomes[0]))  # byte-identical idempotent re-append
    _write_outcomes_journal(data_root, outcomes)
    records = bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")
    assert records["__duplicate_count__"] == 1


def test_malformed_outcome_record_fails_closed(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(6)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    outcomes = [_outcome_payload(signal_id=sid, index=i) for i, sid in enumerate(real_ids)]
    outcomes[0]["outcome"] = "MAYBE"  # not in VALID_OUTCOMES
    _write_outcomes_journal(data_root, outcomes)
    with pytest.raises(bootstrap_module.BootstrapGapError, match="malformed outcome record"):
        bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")


def test_undersized_matched_set_fails_closed(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(8)]
    _write_candidate_journal(data_root, payloads)
    real_ids = [_real_signal_id(p) for p in payloads]
    outcomes = [_outcome_payload(signal_id=sid, index=i) for i, sid in enumerate(real_ids[:4])]
    _write_outcomes_journal(data_root, outcomes)
    key = bootstrap_module.CoverageKey(
        symbol=_SYMBOL, timeframe=_TIMEFRAME, setup_family=_SETUP_FAMILY, action_scope=_ACTION
    )
    candidates = bootstrap_module.select_candidates_by_scope(
        data_root / "signal_intelligence_v1_16" / "candidates.jsonl", key
    )
    records = bootstrap_module.load_outcome_records(data_root / "signal_intelligence_v1_16" / "outcomes.jsonl")
    with pytest.raises(bootstrap_module.BootstrapGapError):
        bootstrap_module.bind_dataset_to_outcome_journal_scope(candidates, records, key=key)


def test_zero_matched_outcomes_fails_closed(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "data"
    payloads = [_candidate_payload(index=i) for i in range(6)]
    _write_candidate_journal(data_root, payloads)
    _write_outcomes_journal(
        data_root, [_outcome_payload(signal_id=f"TM-UNRELATED-{i:04d}", index=i) for i in range(6)]
    )
    argv = _full_argv(data_root)
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "zero outcome records matched" in captured.err
    assert not (data_root / "ser8_registry.db").exists()


# ---------------------------------------------------------------------------
# 11. Human still chooses primary metric.
# ---------------------------------------------------------------------------


def test_primary_metric_must_be_genuinely_available(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root, **{"--primary-metric": "invented_metric"})
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "not one of the genuinely available numeric metrics" in captured.err
    assert "net_r" in captured.err


def test_no_hidden_default_for_any_research_parameter() -> None:
    parser = bootstrap_module.build_arg_parser()
    for action in parser._actions:
        if action.dest in bootstrap_module._RESEARCH_PARAMETER_ARGS:
            assert action.default is None, f"{action.dest} must have no hidden default"


# ---------------------------------------------------------------------------
# 12/13. Review writes no real state; --approve required for FROZEN.
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
    assert "SCOPE CANDIDATES: 8" in captured.out
    assert "MATCHED OUTCOMES: 8" in captured.out
    assert "UNMATCHED CANDIDATES: 0" in captured.out
    assert "DUPLICATE OUTCOMES: 0" in captured.out
    assert "DATASET HASH: sha256:" in captured.out
    assert "AVAILABLE NUMERIC METRICS:" in captured.out
    assert "net_r" in captured.out


def test_approve_required_for_real_creation_and_reaches_frozen(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv)
    assert not (data_root / "ser8_registry.db").exists()

    exit_code = bootstrap_module.main(argv + ["--approve"])
    assert exit_code == 0
    captured = capsys.readouterr()
    hypothesis_id = next(line for line in captured.out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()
    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    assert registry.get(hypothesis_id).state is HypothesisState.FROZEN
    assert (data_root / "ser8_bootstrap_datasets").is_dir()
    assert "run_ser8_real_demo_pipeline.py" in captured.out
    assert "--account 67206924" in captured.out


def test_review_and_approve_identical_hypothesis_and_dataset_hash(genuine_scope: Path, capsys) -> None:
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


# ---------------------------------------------------------------------------
# 14/15. No broker execution; authoritative lifecycle unchanged/unreachable
# past FROZEN.
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


def test_no_numeric_literal_hardcoded_in_bind_function() -> None:
    """bind_dataset_to_outcome_journal_scope must only ever COPY real
    outcome payload values; it must never invent a numeric metric."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "bind_dataset_to_outcome_journal_scope"
    )
    for node in ast.walk(function):
        if isinstance(node, ast.Dict) and node.values:
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
                    pytest.fail(
                        "bind_dataset_to_outcome_journal_scope must never hardcode a numeric dict value"
                    )
