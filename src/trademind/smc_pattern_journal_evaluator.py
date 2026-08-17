"""SMC pattern-journal evaluator: the first production EvaluatorBinding wiring.

This module is the minimal ``ExecutionEvaluator``/``HoldoutEvaluator`` Protocol
implementation needed to run Experiment Execution Runtime V1 and Final Holdout
Evaluation V1 against a real trading hypothesis, once a real historical signal
journal is supplied. It invents no new strategy, backtest, or statistics
logic: every scientific computation is delegated unchanged to
``trademind.validation.validate_rows`` (chronological early/late stability
check, minimum-sample gating, 95% mean confidence interval) and
``trademind.smc_stats``' ATR-normalized win-rate/average-net computation --
the exact machinery ``signal_statistics_report.py`` already uses in
production to validate SMC pattern journals exported by the live signal
system (``data/journal_ecn/signals.csv`` convention).

Dataset format this test_family consumes: a signal-outcome journal CSV in
that same production schema (``signal_time``, ``symbol``, ``timeframe``,
``action``, ``outcome_<horizon>``, ``atr``, ``net_move_<horizon>`` or
``progress_atr_<horizon>``, ...), with one addition -- a ``time`` column
equal to ``signal_time`` -- so the Discovery Engine's existing generic
chronological CSV reader (which requires a ``time`` column; see
``trademind.experiment_execution_runtime``, unmodified) can split it without
any change to that closed module. This evaluator does not read or generate
raw MT5 candles itself; the journal it consumes is the downstream artifact
the live signal-generation/forward-outcome-tracking system already produces
by running against real MT5-exported price data over time.

No friction model is declared or applied here: the journal's
``progress_atr_<horizon>``/``net_move_<horizon>`` values are already net of
whatever spread/commission/slippage the live system applied when each row
was written, so a manifest using this test_family should declare
``trading_friction=None`` -- there is nothing left for this evaluator to
apply, and declaring a fictional single static friction model over an
already-realized, per-row cost would misrepresent the data.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Mapping

from trademind.discovery.manifest import ExperimentManifestV2
from trademind.experiment_execution_contract import EvaluatorBinding, ExecutionPhase
from trademind.validation import validate_rows

TEST_FAMILY = "smc-pattern-journal-validation-v1"
EVALUATOR_ID = "smc-pattern-journal-evaluator-v1"
EVALUATOR_VERSION = "1"
HOLDOUT_EVALUATOR_ID = "smc-pattern-journal-holdout-evaluator-v1"

# Deliberately minimal: exactly the metrics a manifest's predeclared criteria
# would reference. profit_factor_atr is excluded because validate_rows can
# return +inf for it (no losing trades observed), which the execution
# contract's strict finite-number rule would reject outright; a future
# evaluator revision can add a documented capping policy if that metric is
# ever needed by a predeclared criterion.
SUPPORTED_METRICS = ("trades", "win_rate", "avg_net_atr")
PRIMARY_METRIC = "avg_net_atr"


class SMCPatternJournalConfigError(ValueError):
    """Raised when predeclared journal-evaluation parameters are missing or invalid."""


def _required_semantic_int(manifest: ExperimentManifestV2, key: str, *, minimum: int) -> int:
    value = manifest.semantic_parameters.get(key)
    if type(value) is not int or value < minimum:
        raise SMCPatternJournalConfigError(
            f"manifest.semantic_parameters[{key!r}] must be a predeclared integer >= {minimum}"
        )
    return value


def _journal_metrics(
    rows: list[dict[str, str]],
    *,
    horizon: int,
    candidate_minimum: int,
    research_minimum: int,
) -> dict[str, float]:
    """Reuse the closed validate_rows/_normalized_metrics stack unchanged."""
    result = validate_rows(
        rows,
        horizon,
        candidate_minimum=candidate_minimum,
        research_minimum=research_minimum,
    )
    metrics = {
        "trades": float(result.total.trades),
        "win_rate": float(result.total.win_rate),
        "avg_net_atr": float(result.total.avg_net_atr),
    }
    for name, value in metrics.items():
        if not math.isfinite(value):
            raise SMCPatternJournalConfigError(f"computed metric {name!r} is not finite")
    return metrics


class SMCPatternJournalExecutionEvaluator:
    """Discovery/validation-phase ExecutionEvaluator: public journal rows only.

    Sample-size and horizon thresholds are read from the frozen manifest's
    ``semantic_parameters`` -- predeclared before any result is seen, never
    chosen ad hoc per run.
    """

    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def evaluate(
        self,
        rows,
        *,
        manifest: ExperimentManifestV2,
        execution_phase: ExecutionPhase,
    ) -> Mapping[str, int | float]:
        del execution_phase
        horizon = _required_semantic_int(manifest, "horizon", minimum=1)
        candidate_minimum = _required_semantic_int(manifest, "candidate_minimum", minimum=1)
        research_minimum = _required_semantic_int(
            manifest, "research_minimum", minimum=candidate_minimum
        )
        journal_rows = [dict(row.fields) for row in rows]
        return _journal_metrics(
            journal_rows,
            horizon=horizon,
            candidate_minimum=candidate_minimum,
            research_minimum=research_minimum,
        )


class SMCPatternJournalHoldoutEvaluator:
    """Final-holdout HoldoutEvaluator: receives raw decrypted plaintext bytes.

    Bound at seal time to its own frozen ``evaluator_id`` and source hash,
    exactly like every other ``FinalHoldoutRunner`` evaluator. The holdout
    runner never exposes the manifest to the evaluator, so whoever configures
    and seals this evaluator is responsible for supplying the same
    horizon/sample-size thresholds predeclared on the manifest that
    authorized the evaluation.
    """

    evaluator_id = HOLDOUT_EVALUATOR_ID

    def __init__(self, *, horizon: int, candidate_minimum: int, research_minimum: int) -> None:
        if type(horizon) is not int or horizon < 1:
            raise SMCPatternJournalConfigError("horizon must be a positive integer")
        if type(candidate_minimum) is not int or candidate_minimum < 1:
            raise SMCPatternJournalConfigError("candidate_minimum must be a positive integer")
        if type(research_minimum) is not int or research_minimum < candidate_minimum:
            raise SMCPatternJournalConfigError("research_minimum must be >= candidate_minimum")
        self.horizon = horizon
        self.candidate_minimum = candidate_minimum
        self.research_minimum = research_minimum

    def evaluate(self, plaintext: bytes) -> Mapping[str, int | float]:
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SMCPatternJournalConfigError(
                "final holdout journal must be valid UTF-8 CSV"
            ) from exc
        rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
        return _journal_metrics(
            rows,
            horizon=self.horizon,
            candidate_minimum=self.candidate_minimum,
            research_minimum=self.research_minimum,
        )


def binding() -> EvaluatorBinding:
    """The production EvaluatorBinding for this test_family.

    No friction model is declared (see module docstring): the journal's
    values are already net of whatever cost the live system applied.
    """
    return EvaluatorBinding(
        test_family=TEST_FAMILY,
        evaluator_id=EVALUATOR_ID,
        evaluator_version=EVALUATOR_VERSION,
        supported_metrics=SUPPORTED_METRICS,
        supported_friction_models=(),
        deterministic=True,
    )


__all__ = [
    "EVALUATOR_ID",
    "EVALUATOR_VERSION",
    "HOLDOUT_EVALUATOR_ID",
    "PRIMARY_METRIC",
    "SUPPORTED_METRICS",
    "TEST_FAMILY",
    "SMCPatternJournalConfigError",
    "SMCPatternJournalExecutionEvaluator",
    "SMCPatternJournalHoldoutEvaluator",
    "binding",
]
