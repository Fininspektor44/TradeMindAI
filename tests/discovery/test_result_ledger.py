import json

from trademind.discovery.result_ledger import LedgerIntegrityError, ResultLedger


def test_hash_chain_detects_payload_tamper_and_tail_deletion(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = ResultLedger(path)
    ledger.append({"hypothesis_id": "H1", "status": "TRAIN_TESTED"})
    ledger.append({"hypothesis_id": "H1", "status": "VALIDATION_PASSED"})
    assert ledger.verify()

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["status"] = "ACCEPTED"
    lines[0] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not ledger.verify()

    try:
        ledger.append({"x": 1})
    except LedgerIntegrityError:
        pass
    else:
        raise AssertionError("broken ledger must block append")


def test_tail_truncation_breaks_external_head_anchor(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = ResultLedger(path)
    ledger.append({"n": 1})
    ledger.append({"n": 2})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")
    assert not ledger.verify()
