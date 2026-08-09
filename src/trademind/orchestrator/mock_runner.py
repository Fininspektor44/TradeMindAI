"""Local end-to-end validation runner for Orchestrator v1 without real model APIs."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .agent_protocol import AgentDecision, AgentEnvelope, AgentResult
from .artifact_store import ArtifactStore
from .budget import BudgetManager
from .control_plane import ControlPlane
from .engine import WorkflowEngine
from .models import Role, Task, TaskState
from .role_router import RoleRouter
from .tool_runner import CommandTemplate, ToolRunner


class DeterministicMockProvider:
    """Schema-correct local stand-in used only to validate orchestration mechanics."""

    def __init__(self, role: Role) -> None:
        self.role = role
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "local-mock"

    @property
    def model_name(self) -> str:
        return f"deterministic-{self.role.value.lower()}"

    def execute(self, envelope: AgentEnvelope) -> AgentResult:
        if envelope.role is not self.role:
            return AgentResult(
                success=False,
                summary="role mismatch",
                error=f"expected {self.role.value}, got {envelope.role.value}",
            )
        self.calls += 1
        decision = AgentDecision.APPROVE if self.role is Role.AUDITOR else AgentDecision.CONTINUE
        return AgentResult(
            success=True,
            summary=f"{self.role.value} completed {envelope.required_output_schema}",
            output_schema=envelope.required_output_schema,
            tokens=1,
            cost=0.0,
            decision=decision,
        )


@dataclass(frozen=True, slots=True)
class MockRunReport:
    run_id: str
    task_id: str
    verdict: str
    final_state: str
    audit_chain_valid: bool
    model_calls: int
    artifact_count: int
    state_directory: str
    database_path: str
    completed_at: str


def _default_state_dir(run_id: str) -> Path:
    # Keep this intentionally short. Windows installations without long-path
    # support can otherwise fail while pytest creates nested tmp_path fixtures.
    return Path(tempfile.gettempdir()) / "tmai-mock" / run_id


def _default_test_template() -> CommandTemplate:
    basetemp = Path(tempfile.gettempdir()) / f"tmai-inner-{uuid.uuid4().hex[:8]}"
    return CommandTemplate(
        executable=sys.executable,
        args=(
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
            "tests/orchestrator",
        ),
        timeout_seconds=180.0,
    )


def run_mock_cycle(
    *,
    repo_root: str | Path,
    state_dir: str | Path | None = None,
    test_template: CommandTemplate | None = None,
) -> MockRunReport:
    """Run the full role-separated workflow using deterministic local mock providers."""
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repo root does not exist: {root}")
    if not (root / "pyproject.toml").is_file():
        raise ValueError(f"repo root does not contain pyproject.toml: {root}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    task_id = f"mock-{run_id}"
    state_root = Path(state_dir).expanduser().resolve() if state_dir else _default_state_dir(run_id)
    state_root.mkdir(parents=True, exist_ok=True)
    db_path = state_root / "orchestrator.db"

    control = ControlPlane(db_path)
    providers = {
        Role.ARCHITECT: DeterministicMockProvider(Role.ARCHITECT),
        Role.DEVELOPER: DeterministicMockProvider(Role.DEVELOPER),
        Role.AUDITOR: DeterministicMockProvider(Role.AUDITOR),
    }
    router = RoleRouter(providers)
    budget = BudgetManager(
        db_path,
        daily_cost_ceiling=1.0,
        monthly_cost_ceiling=10.0,
        per_task_call_limit=10,
        per_role_call_limit=20,
        failure_cooldown_seconds=0,
        daily_token_ceiling=1_000,
        monthly_token_ceiling=10_000,
    )
    artifacts = ArtifactStore(state_root / "artifacts")
    tools = ToolRunner(
        allowed_roots=(root,),
        templates={"orchestrator-tests": test_template or _default_test_template()},
    )
    engine = WorkflowEngine(
        control=control,
        router=router,
        budget=budget,
        artifacts=artifacts,
        tools=tools,
        working_directory=str(root),
        estimated_model_cost=0.0,
        estimated_model_tokens=1,
    )

    control.create_task(
        Task.new(
            task_id=task_id,
            goal="validate Orchestrator v1 end-to-end mechanics with local deterministic mocks",
            scope=("src/trademind/orchestrator", "tests/orchestrator"),
            budget_limit=1.0,
            acceptance_criteria=(
                "role-separated workflow reaches COMPLETED",
                "focused orchestrator tests pass",
                "audit hash-chain verifies",
            ),
        )
    )
    final_task = engine.run_until_stopped(task_id)
    audit_ok = control.audit_log.verify()
    total_provider_calls = sum(provider.calls for provider in providers.values())
    verdict = "PASS" if final_task.state is TaskState.COMPLETED and audit_ok else "FAIL"

    report = MockRunReport(
        run_id=run_id,
        task_id=task_id,
        verdict=verdict,
        final_state=final_task.state.value,
        audit_chain_valid=audit_ok,
        model_calls=total_provider_calls,
        artifact_count=len(final_task.artifact_refs),
        state_directory=str(state_root),
        database_path=str(db_path),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    report_path = state_root / "mock_run_report.json"
    report_path.write_text(
        json.dumps(asdict(report), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the TradeMindAI Orchestrator v1 local mock validation cycle."
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="TradeMindAI repository root. Defaults to the current source checkout.",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Optional directory for SQLite state and evidence artifacts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_mock_cycle(repo_root=args.repo_root, state_dir=args.state_dir)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "verdict": "ERROR",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(json.dumps(asdict(report), indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
