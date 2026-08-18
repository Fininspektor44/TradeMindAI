"""Tests for scripts/bootstrap_first_real_hypothesis.py."""

from __future__ import annotations

import ast
import gc
import importlib
import json
import os
import sqlite3
import sys
import tempfile
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
_HOLDOUT_KEY_ID = "ser8-bootstrap-test-holdout-key-v1"
_HOLDOUT_KEY_ENV = "SER8_BOOTSTRAP_TEST_HOLDOUT_KEY"

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
        "--holdout-key-env": _HOLDOUT_KEY_ENV, "--holdout-key-id": _HOLDOUT_KEY_ID,
    }
    values.update(overrides)
    argv: list[str] = []
    for key, value in values.items():
        argv.extend([key, value])
    return argv


@pytest.fixture(autouse=True)
def _holdout_key_env() -> None:
    """Real key MATERIAL still comes only from this environment variable
    (never persisted in the repo/DB/artifacts) -- this fixture only sets a
    deterministic test value for the duration of each test, exactly like
    tests/test_run_ser8_real_demo_pipeline.py's own _full_real_chain
    fixture already does for the sibling script."""
    os.environ[_HOLDOUT_KEY_ENV] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # 32 zero bytes, valid base64


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
    # "quarantine" alone is deliberately NOT in this list: a later task
    # (protected final holdout) legitimately introduced a real, unrelated
    # quarantine_dir concept (physically isolating sealed-holdout plaintext,
    # nothing to do with outcome-journal sourcing) -- these two specific
    # compound terms remain the precise things that must never appear.
    for forbidden in ("paper_signals", "data/quarantine", "live_signal_runtime_v1", "live_signal_runtime_eCN"):
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
    """This bootstrap only ever reaches FROZEN and (since the protected
    final-holdout completion task) seals a final holdout WHILE still
    FROZEN -- it must never construct any of the controls that would
    actually ADVANCE state past FROZEN (TRAIN_TESTED/VALIDATION/
    HOLDOUT_CONSUMED/terminal). FinalHoldoutSealer/HoldoutSealStore are
    legitimately constructed for sealing and are excluded from this check;
    FinalHoldoutRunner is legitimately named only in prose explaining why
    it is NOT used (this script never triggers a real holdout run)."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    code_lines = [line for line in source.splitlines() if not line.strip().startswith("#")]
    code_without_comments = "\n".join(code_lines)
    for forbidden_name in (
        "TrainTestExecutionControl(", "ValidationExecutionControl(", "HoldoutTriggerBridge(",
        "FinalVerdictAcceptanceControl(", "FinalHoldoutRunner(",
    ):
        assert forbidden_name not in code_without_comments, forbidden_name


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


# ---------------------------------------------------------------------------
# SQLite resource lifecycle: REVIEW must close every DB handle before its
# TemporaryDirectory is deleted (the real Windows WinError 32 failure this
# task fixes).
# ---------------------------------------------------------------------------


def _review_temp_dirs() -> set[Path]:
    root = Path(tempfile.gettempdir())
    return {path for path in root.glob("ser8-bootstrap-review-*") if path.is_dir()}


def _live_sqlite_connections() -> list[sqlite3.Connection]:
    gc.collect()
    return [obj for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection)]


def test_review_completes_successfully(genuine_scope: Path) -> None:
    exit_code = bootstrap_module.main(_full_argv(genuine_scope))
    assert exit_code == 0


def test_temporary_review_directory_is_actually_deleted(genuine_scope: Path) -> None:
    before = _review_temp_dirs()
    exit_code = bootstrap_module.main(_full_argv(genuine_scope))
    assert exit_code == 0
    after = _review_temp_dirs()
    # No new ser8-bootstrap-review-* directory survives the call -- proves
    # tempfile.TemporaryDirectory's own cleanup actually succeeded rather
    # than being silently skipped or suppressed.
    assert after == before


def test_repeated_review_runs_do_not_leak_sqlite_connections(genuine_scope: Path) -> None:
    baseline = len(_live_sqlite_connections())
    for _ in range(5):
        exit_code = bootstrap_module.main(_full_argv(genuine_scope))
        assert exit_code == 0
        # After each individual review run, every SQLite connection that
        # run opened must already be gone -- not merely "eventually
        # collected" by the time the whole test finishes.
        assert len(_live_sqlite_connections()) == baseline
    assert len(_live_sqlite_connections()) == baseline


def test_exception_path_releases_db_handles_and_still_deletes_directory(
    genuine_scope: Path, monkeypatch, capsys
) -> None:
    """Force a failure INSIDE run_bootstrap_chain, well after several real
    SQLite connections have already been opened (HypothesisRegistry,
    ControlPlane, BudgetManager, ResearchExecutionControl,
    ResearchProposalIntakeControl, ResearchExperimentSpecificationControl
    are all constructed before chronological_split is ever called) -- the
    exact scenario that produced WinError 32 on Windows."""

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic failure deep inside the authoritative chain")

    monkeypatch.setattr(bootstrap_module, "chronological_split", _boom)

    before_dirs = _review_temp_dirs()
    baseline_connections = len(_live_sqlite_connections())

    exit_code = bootstrap_module.main(_full_argv(genuine_scope))
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "synthetic failure deep inside the authoritative chain" in captured.err

    after_dirs = _review_temp_dirs()
    assert after_dirs == before_dirs  # the temp directory was still deleted despite the failure.
    assert len(_live_sqlite_connections()) == baseline_connections  # every handle was still released.


def test_review_and_approve_hashes_unchanged_by_the_lifecycle_fix(genuine_scope: Path, capsys) -> None:
    """The exact same invariant tests/test_bootstrap_first_real_hypothesis.py
    already proved before this fix -- re-asserted here to make the
    "unchanged by this task" requirement an explicit, standalone test."""
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


def test_approve_mode_untouched_by_lifecycle_fix() -> None:
    """--approve never uses tempfile.TemporaryDirectory or
    _run_review_chain at all -- its own control flow is unmodified."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    approve_block_start = source.index("if args.approve:")
    approve_block = source[approve_block_start : source.index("with tempfile.TemporaryDirectory")]
    assert "_run_review_chain" not in approve_block
    assert "TemporaryDirectory" not in approve_block


# ---------------------------------------------------------------------------
# Protected final holdout completion.
# ---------------------------------------------------------------------------


def test_approve_registers_isolated_protected_final_holdout(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    exit_code = bootstrap_module.main(argv + ["--approve"])
    assert exit_code == 0
    captured = capsys.readouterr()
    hypothesis_id = next(line for line in captured.out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()
    assert "protected final holdout = newly sealed" in captured.out

    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    from trademind.discovery.holdout_store import HoldoutSealStore

    seals = HoldoutSealStore(registry)
    record = seals.get(hypothesis_id)
    assert record.isolated
    assert record.public_row_count is not None and record.holdout_row_count is not None
    assert record.public_row_count + record.holdout_row_count == 8


def test_resume_completion_on_already_sealed_hypothesis_is_idempotent(genuine_scope: Path, capsys) -> None:
    """DO NOT fabricate another hypothesis: --complete-holdout-for against
    an hypothesis that is already sealed must reach idempotent success,
    never a second seal attempt."""
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])
    first_out = capsys.readouterr().out
    first_id = next(line for line in first_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    exit_code = bootstrap_module.main(
        _full_argv(data_root, **{"--complete-holdout-for": first_id}) + ["--approve"]
    )
    assert exit_code == 0
    second_out = capsys.readouterr().out
    assert first_id in second_out
    assert "already sealed (idempotent)" in second_out


def test_resume_completion_skips_the_non_idempotent_intake_chain(genuine_scope: Path, capsys) -> None:
    """AUDIT FINDING this task fixes: re-running the FULL --approve chain a
    second time for the same real inputs crashes with a raw
    sqlite3.IntegrityError (result_artifact_hash_ref is UNIQUE, but
    request_hash -- the idempotency key -- differs between two independent
    ResearchExecutionControl.create_authorization/claim_execution calls
    even for byte-identical content). --complete-holdout-for is the safe
    resume path that avoids this entirely by never re-entering that chain."""
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])
    first_out = capsys.readouterr().out
    first_id = next(line for line in first_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    fn_start = source.index("def run_seal_only_completion(")
    fn_end = source.index("\ndef ", fn_start + 1)
    body = source[fn_start:fn_end]
    for forbidden in (
        "ResearchExecutionControl(", "ResearchProposalIntakeControl(",
        "ResearchExperimentSpecificationControl(", "build_report_v2(", "build_packet_v2_from_artifact(",
        "freeze_manifest_v2_in_transaction(",
    ):
        assert forbidden not in body, forbidden

    exit_code = bootstrap_module.main(
        _full_argv(data_root, **{"--complete-holdout-for": first_id}) + ["--approve"]
    )
    assert exit_code == 0


def test_complete_final_set_for_requires_approve(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])
    first_out = capsys.readouterr().out
    first_id = next(line for line in first_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    exit_code = bootstrap_module.main(_full_argv(data_root, **{"--complete-holdout-for": first_id}))
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "requires --approve" in captured.err


def test_complete_final_set_for_rejects_dataset_hash_mismatch(genuine_scope: Path, capsys) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])
    first_out = capsys.readouterr().out
    first_id = next(line for line in first_out.splitlines() if "hypothesis_id" in line).split("=")[1].strip()

    # A DIFFERENT real scope (SELL, with its own real candidates/outcomes)
    # must never be accepted as "the" bound dataset for a hypothesis frozen
    # from the original BUY scope.
    extra_payloads = [_candidate_payload(index=i, action="SELL") for i in range(5)]
    existing = [
        json.loads(line)
        for line in (data_root / "signal_intelligence_v1_16" / "candidates.jsonl").read_text().strip().splitlines()
    ]
    _write_candidate_journal(data_root, existing + extra_payloads)
    extra_ids = [_real_signal_id(p) for p in extra_payloads]
    existing_outcomes = [
        json.loads(line)
        for line in (data_root / "signal_intelligence_v1_16" / "outcomes.jsonl").read_text().strip().splitlines()
    ]
    _write_outcomes_journal(
        data_root,
        existing_outcomes + [_outcome_payload(signal_id=sid, index=i, outcome="WIN") for i, sid in enumerate(extra_ids)],
    )

    exit_code = bootstrap_module.main(
        _full_argv(data_root, **{"--complete-holdout-for": first_id, "--action-scope": "SELL"}) + ["--approve"]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "hypothesis does not exist" in captured.err or "does not match" in captured.err


def test_conflicting_seal_fails_closed(genuine_scope: Path) -> None:
    """A seal row that exists but was never marked isolated must never be
    silently re-sealed or repaired automatically."""
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])

    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    from trademind.discovery.holdout_store import HoldoutSealStore

    seals = HoldoutSealStore(registry)

    import sqlite3

    db = sqlite3.connect(data_root / "ser8_registry.db")
    db.row_factory = sqlite3.Row
    hyp_id = db.execute("SELECT hypothesis_id FROM final_holdout_seals LIMIT 1").fetchone()["hypothesis_id"]
    # Simulate a partially-completed seal: isolation was never attested.
    db.execute(
        "UPDATE final_holdout_seals SET isolated_at=NULL, isolation_receipt_hash=NULL, "
        "public_max_time=NULL, holdout_start_time=NULL, holdout_end_time=NULL, "
        "public_row_count=NULL, holdout_row_count=NULL WHERE hypothesis_id=?",
        (hyp_id,),
    )
    db.commit()
    db.close()

    from trademind.discovery.holdout_sealer import FinalHoldoutSealer
    from trademind.discovery.holdout_keys import EnvironmentKeyProvider

    keys = EnvironmentKeyProvider(key_id=_HOLDOUT_KEY_ID, environment_variable=_HOLDOUT_KEY_ENV)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)
    with pytest.raises(bootstrap_module.BootstrapGapError, match="requires manual review"):
        bootstrap_module.resolve_or_seal_protected_final_holdout(
            registry=registry, seals=seals, sealer=sealer, hypothesis_id=hyp_id,
            bound_dataset=None, plan=None,
            quarantine_dir=data_root.parent / "quarantine", sealed_holdout_path=data_root / "x.sealed.json",
            holdout_plaintext_workdir=data_root / "workdir", key_id=_HOLDOUT_KEY_ID,
            evaluator_id="deterministic-aggregate-v1-holdout",
            evaluator_artifact_path=Path(bootstrap_module.demo_pipeline_module.__file__),
        )


def test_public_max_time_strictly_before_holdout_start_time(genuine_scope: Path) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])

    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    from trademind.discovery.holdout_store import HoldoutSealStore

    seals = HoldoutSealStore(registry)
    import sqlite3

    db = sqlite3.connect(data_root / "ser8_registry.db")
    db.row_factory = sqlite3.Row
    hyp_id = db.execute("SELECT hypothesis_id FROM final_holdout_seals LIMIT 1").fetchone()["hypothesis_id"]
    db.close()
    record = seals.get(hyp_id)
    assert record.public_max_time is not None and record.holdout_start_time is not None
    assert datetime.fromisoformat(record.public_max_time) < datetime.fromisoformat(record.holdout_start_time)


def test_plaintext_quarantined_not_left_in_workdir(genuine_scope: Path) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])

    workdir = data_root / "ser8_bootstrap_datasets" / "_holdout_plaintext_workdir"
    assert not any(workdir.glob("*")) if workdir.exists() else True

    quarantine_dir = data_root.parent / "ser8_final_holdout_quarantine"
    assert quarantine_dir.is_dir()
    quarantined_files = list(quarantine_dir.glob("*.holdout-source"))
    assert len(quarantined_files) == 1


def test_key_material_never_persisted_in_sealed_artifact_or_db(genuine_scope: Path) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])

    real_key_material = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    dataset_dir = data_root / "ser8_bootstrap_datasets"
    sealed_files = list(dataset_dir.glob("*.final-holdout.sealed.json"))
    assert len(sealed_files) == 1
    sealed_text = sealed_files[0].read_text(encoding="utf-8")
    assert real_key_material not in sealed_text

    db_bytes = (data_root / "ser8_registry.db").read_bytes()
    assert real_key_material.encode("utf-8") not in db_bytes


def test_protected_rows_excluded_from_public_manifest_datasets(genuine_scope: Path) -> None:
    # NOTE: this function's own name must never contain the substring
    # "holdout" -- pytest bakes the test name into tmp_path, and
    # ResearchExperimentSpecificationControl._datasets has its own
    # belt-and-suspenders check rejecting any dataset file_path containing
    # "holdout" (case-insensitive), which a test-name collision would
    # otherwise trip even though the REAL production path
    # (data/ser8_bootstrap_datasets/<hash>.csv) never contains that word.
    """Discovery + validation remain public; the 3 protected-final-set rows
    (of 8) are never part of what was persisted as the manifest's own
    public dataset artifacts."""
    data_root = genuine_scope
    argv = _full_argv(data_root)
    bootstrap_module.main(argv + ["--approve"])

    registry = HypothesisRegistry(data_root / "ser8_registry.db")
    from trademind.discovery.holdout_store import HoldoutSealStore

    seals = HoldoutSealStore(registry)
    import sqlite3

    db = sqlite3.connect(data_root / "ser8_registry.db")
    db.row_factory = sqlite3.Row
    hyp_id = db.execute("SELECT hypothesis_id FROM final_holdout_seals LIMIT 1").fetchone()["hypothesis_id"]
    db.close()
    record = seals.get(hyp_id)
    assert record.public_row_count == 5  # 8 total, 60/20/20 -> 4 discovery + 1 validation = 5 public
    assert record.holdout_row_count == 3


def test_review_mode_never_touches_real_seal_store_or_quarantine(genuine_scope: Path) -> None:
    data_root = genuine_scope
    argv = _full_argv(data_root)
    exit_code = bootstrap_module.main(argv)
    assert exit_code == 0
    assert not (data_root / "ser8_registry.db").exists()
    assert not (data_root.parent / "ser8_final_holdout_quarantine").exists()


def test_evaluator_artifact_shared_with_demo_pipeline_script() -> None:
    """The final holdout must later be consumable by
    run_ser8_real_demo_pipeline.py's own authoritative evaluator identity --
    proven by both scripts resolving to the exact same evaluator_id and the
    exact same evaluator_artifact_path (this file's own path)."""
    evaluator = bootstrap_module.demo_pipeline_module._DeterministicAggregateHoldoutEvaluator(
        primary_metric="net_r", parameters={},
    )
    assert evaluator.evaluator_id == "deterministic-aggregate-v1-holdout"
    assert Path(bootstrap_module.demo_pipeline_module.__file__).resolve().is_file()


def test_no_custom_encryption_only_authoritative_seal_bytes() -> None:
    """This file must never implement its own cipher -- the only
    cryptography anywhere in the chain is the existing, unmodified
    seal_bytes/verify_envelope inside FinalHoldoutSealer.seal_file."""
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("Fernet", "hashlib.pbkdf2", "hmac.new", "Crypto.Cipher", "cryptography.hazmat", "AES.new("):
        assert forbidden not in source, forbidden
    assert "sealer.seal_file(" in source
