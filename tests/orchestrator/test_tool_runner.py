import os
import sys

from trademind.orchestrator.tool_runner import CommandTemplate, ToolPolicyError, ToolRunner


def _runner(tmp_path, templates, *, allowed_environment=("PATH",)):
    return ToolRunner(
        allowed_roots=(tmp_path,),
        templates=templates,
        allowed_environment=allowed_environment,
    )


def test_registered_template_runs_without_shell(tmp_path):
    runner = _runner(
        tmp_path,
        {
            "hello": CommandTemplate(
                executable=sys.executable,
                args=("-c", "print('hello')"),
            )
        },
    )
    result = runner.run("hello", cwd=tmp_path)
    assert result.success
    assert result.stdout.strip() == "hello"
    assert result.command[0] == sys.executable


def test_unknown_template_is_rejected(tmp_path):
    runner = _runner(tmp_path, {})
    try:
        runner.run("model_supplied_shell", cwd=tmp_path)
    except ToolPolicyError:
        pass
    else:
        raise AssertionError("unregistered commands must never execute")


def test_working_directory_cannot_escape_allow_list(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    runner = ToolRunner(
        allowed_roots=(allowed,),
        templates={"hello": CommandTemplate(sys.executable, ("-c", "print('x')"))},
    )
    try:
        runner.run("hello", cwd=outside)
    except ToolPolicyError:
        pass
    else:
        raise AssertionError("tool cwd must stay inside an allowed root")


def test_secret_environment_is_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEMIND_SECRET_TOKEN", "do-not-leak")
    code = "import os; print(os.getenv('TRADEMIND_SECRET_TOKEN', 'ABSENT'))"
    runner = _runner(
        tmp_path,
        {"env-check": CommandTemplate(sys.executable, ("-c", code))},
        allowed_environment=("PATH",),
    )
    result = runner.run("env-check", cwd=tmp_path)
    assert result.success
    assert result.stdout.strip() == "ABSENT"
    assert "do-not-leak" not in result.stdout
    assert "do-not-leak" not in result.stderr


def test_timeout_is_reported_without_raising(tmp_path):
    runner = _runner(
        tmp_path,
        {
            "slow": CommandTemplate(
                sys.executable,
                ("-c", "import time; time.sleep(0.2)"),
                timeout_seconds=0.02,
            )
        },
    )
    result = runner.run("slow", cwd=tmp_path)
    assert result.timed_out
    assert result.exit_code is None
    assert not result.success


def test_os_environment_secret_exists_only_in_parent(monkeypatch):
    monkeypatch.setenv("ANOTHER_SECRET", "parent-only")
    assert os.environ["ANOTHER_SECRET"] == "parent-only"
