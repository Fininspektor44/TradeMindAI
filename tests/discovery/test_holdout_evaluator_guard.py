from pathlib import Path

import pytest

from trademind.discovery.holdout_runner import HoldoutRunError, _evaluate_with_side_effect_guard


class QuietEvaluator:
    evaluator_id = "quiet-v1"

    def evaluate(self, plaintext):
        return {"bytes": len(plaintext)}


class PrintingEvaluator:
    evaluator_id = "printing-v1"

    def evaluate(self, plaintext):
        print(plaintext.decode("utf-8"))
        return {"bytes": len(plaintext)}


class FileWritingEvaluator:
    evaluator_id = "file-writing-v1"

    def __init__(self, destination):
        self.destination = destination

    def evaluate(self, plaintext):
        Path(self.destination).write_bytes(plaintext)
        return {"bytes": len(plaintext)}


def test_quiet_evaluator_is_allowed():
    assert _evaluate_with_side_effect_guard(QuietEvaluator(), b"secret\n") == {"bytes": 7}


def test_stdout_attempt_is_blocked_without_exposing_plaintext(capsys):
    with pytest.raises(HoldoutRunError, match="stdout/stderr"):
        _evaluate_with_side_effect_guard(PrintingEvaluator(), b"TOP-SECRET\n")
    captured = capsys.readouterr()
    assert "TOP-SECRET" not in captured.out
    assert "TOP-SECRET" not in captured.err


def test_file_write_attempt_is_blocked(tmp_path):
    destination = tmp_path / "leak.txt"
    with pytest.raises(HoldoutRunError, match="forbidden side effect"):
        _evaluate_with_side_effect_guard(
            FileWritingEvaluator(destination),
            b"TOP-SECRET\n",
        )
    assert not destination.exists()
