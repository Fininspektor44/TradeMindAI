import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trademind.discovery.holdout_crypto import seal_bytes
from trademind.discovery.holdout_runner import FinalHoldoutRunner, HoldoutRunError
from trademind.discovery.hypothesis_registry import HypothesisState
from trademind.discovery.result_ledger import ResultLedger


KEY = bytes(range(32))
FAMILY_ID = "hf_claim_recovery"
MANIFEST_HASH = hashlib.sha256(b"claim-recovery-manifest").hexdigest()
EVALUATOR_ARTIFACT = Path(__file__).resolve()
EVALUATOR_HASH = hashlib.sha256(EVALUATOR_ARTIFACT.read_bytes()).hexdigest()


class CountingEvaluator:
    evaluator_id = "claim-recovery-evaluator-v1"

    def evaluate(self, plaintext):
        return {"rows": plaintext.count(b"\n"), "passed": True}


class StaticKeys:
    def load_key(self, key_id):
        assert key_id == "claim-recovery-key-v1"
        return KEY


class StaticSeals:
    def __init__(self, envelope_hash):
        self.record = SimpleNamespace(
            isolated=True,
            hypothesis_family_id=FAMILY_ID,
            manifest_hash=MANIFEST_HASH,
            evaluator_id=CountingEvaluator.evaluator_id,
            evaluator_hash=EVALUATOR_HASH,
            key_id="claim-recovery-key-v1",
            envelope_hash=envelope_hash,
        )

    def get(self, hypothesis_id):
        assert hypothesis_id == "H-CLAIM-RECOVERY"
        return self.record


class FailOnceRegistry:
    def __init__(self):
        self.state = HypothesisState.VALIDATION_PASSED
        self.fail_next_consumption = True

    def get(self, hypothesis_id):
        assert hypothesis_id == "H-CLAIM-RECOVERY"
        return SimpleNamespace(
            hypothesis_id=hypothesis_id,
            hypothesis_family_id=FAMILY_ID,
            manifest_hash=MANIFEST_HASH,
            state=self.state,
        )

    def family_status(self, family_id):
        assert family_id == FAMILY_ID
        return {
            "holdout_consumed": self.state is HypothesisState.HOLDOUT_CONSUMED,
            "terminal_state": None,
        }

    def transition(self, hypothesis_id, target):
        assert hypothesis_id == "H-CLAIM-RECOVERY"
        assert target is HypothesisState.HOLDOUT_CONSUMED
        if self.fail_next_consumption:
            self.fail_next_consumption = False
            raise RuntimeError("synthetic transient sqlite lock")
        self.state = HypothesisState.HOLDOUT_CONSUMED
        return self.get(hypothesis_id)


def test_transient_registry_failure_before_consumption_does_not_burn_holdout(tmp_path):
    document = seal_bytes(
        b"final-row\n",
        key=KEY,
        key_id="claim-recovery-key-v1",
        hypothesis_family_id=FAMILY_ID,
        manifest_hash=MANIFEST_HASH,
        evaluator_id=CountingEvaluator.evaluator_id,
        evaluator_hash=EVALUATOR_HASH,
    )
    sealed_path = tmp_path / "final.holdout.json"
    sealed_path.write_text(json.dumps(document), encoding="utf-8")

    registry = FailOnceRegistry()
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=StaticSeals(document["envelope_hash"]),
        keys=StaticKeys(),
        ledger=ledger,
        evaluator=CountingEvaluator(),
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )

    with pytest.raises(HoldoutRunError, match="retry is allowed"):
        runner.run_once(
            hypothesis_id="H-CLAIM-RECOVERY",
            sealed_path=sealed_path,
        )

    assert registry.state is HypothesisState.VALIDATION_PASSED
    ledger_text = ledger.path.read_text(encoding="utf-8")
    assert "FINAL_HOLDOUT_INTENT" in ledger_text
    assert "FINAL_HOLDOUT_INTENT_ABORTED" in ledger_text
    assert '"record_type":"FINAL_HOLDOUT_CLAIM"' not in ledger_text

    result = runner.run_once(
        hypothesis_id="H-CLAIM-RECOVERY",
        sealed_path=sealed_path,
    )
    assert registry.state is HypothesisState.HOLDOUT_CONSUMED
    assert result.aggregate_metrics == {"rows": 1, "passed": True}
    assert ledger.verify()
    assert '"record_type":"FINAL_HOLDOUT_CLAIM"' in ledger.path.read_text(encoding="utf-8")
