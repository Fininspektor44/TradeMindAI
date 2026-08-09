import json
import sys

from trademind.orchestrator.mock_runner import run_mock_cycle
from trademind.orchestrator.tool_runner import CommandTemplate


def _repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='mock'\n", encoding="utf-8")
    return tmp_path


def test_mock_runner_completes_with_local_deterministic_providers(tmp_path):
    repo = _repo(tmp_path / "repo")
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text("[project]\nname='mock'\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    report = run_mock_cycle(
        repo_root=repo,
        state_dir=state_dir,
        test_template=CommandTemplate(
            executable=sys.executable,
            args=("-c", "print('operator tests pass')"),
            timeout_seconds=10,
        ),
    )

    assert report.verdict == "PASS"
    assert report.final_state == "COMPLETED"
    assert report.audit_chain_valid
    assert report.model_calls == 5
    assert report.artifact_count == 6

    persisted = json.loads((state_dir / "mock_run_report.json").read_text(encoding="utf-8"))
    assert persisted["verdict"] == "PASS"
    assert persisted["task_id"] == report.task_id


def test_mock_runner_fails_closed_when_operator_test_command_fails(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='mock'\n", encoding="utf-8")

    report = run_mock_cycle(
        repo_root=repo,
        state_dir=tmp_path / "state-fail",
        test_template=CommandTemplate(
            executable=sys.executable,
            args=("-c", "raise SystemExit(7)"),
            timeout_seconds=10,
        ),
    )

    assert report.verdict == "FAIL"
    assert report.final_state == "FAILED"
    assert report.audit_chain_valid
