"""Allow-listed local command execution for the deterministic OPERATOR role."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


class ToolPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandTemplate:
    executable: str
    args: tuple[str, ...] = ()
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.executable.strip():
            raise ValueError("executable must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ToolRunResult:
    template_name: str
    command: tuple[str, ...]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def success(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class ToolRunner:
    """Execute trusted command templates without a shell or model-supplied arguments."""

    def __init__(
        self,
        *,
        allowed_roots: tuple[str | Path, ...],
        templates: Mapping[str, CommandTemplate],
        allowed_environment: tuple[str, ...] = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"),
    ) -> None:
        if not allowed_roots:
            raise ValueError("at least one allowed working-directory root is required")
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
        registered_templates = dict(templates)
        self.allowed_environment = frozenset(allowed_environment)
        if any(not name.strip() for name in registered_templates):
            raise ValueError("tool template names must not be empty")
        self._templates: Mapping[str, CommandTemplate] = MappingProxyType(registered_templates)

    @property
    def templates(self) -> Mapping[str, CommandTemplate]:
        """Read-only registry of trusted command templates."""
        return self._templates

    def _validated_cwd(self, cwd: str | Path) -> Path:
        resolved = Path(cwd).expanduser().resolve()
        if not resolved.is_dir():
            raise ToolPolicyError(f"working directory does not exist: {resolved}")
        if not any(resolved == root or resolved.is_relative_to(root) for root in self.allowed_roots):
            raise ToolPolicyError(f"working directory is outside allow-list: {resolved}")
        return resolved

    def _environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self.allowed_environment
        }

    def _resolve_template(self, template_name: str) -> CommandTemplate:
        template = self._templates.get(template_name)
        if template is None:
            raise ToolPolicyError(f"unknown tool template: {template_name}")
        return template

    def _run_template(
        self,
        template_name: str,
        template: CommandTemplate,
        *,
        cwd: str | Path,
    ) -> ToolRunResult:
        working_directory = self._validated_cwd(cwd)
        command = (template.executable, *template.args)
        try:
            completed = subprocess.run(
                command,
                cwd=working_directory,
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=template.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ToolRunResult(
                template_name=template_name,
                command=command,
                cwd=str(working_directory),
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

        return ToolRunResult(
            template_name=template_name,
            command=command,
            cwd=str(working_directory),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
        )

    def run_allowed(
        self,
        template_name: str,
        *,
        allowed_templates: Collection[str],
        cwd: str | Path,
    ) -> ToolRunResult:
        """Authorize an exact template name, resolve it once, and execute that instance."""
        if template_name not in allowed_templates:
            raise ToolPolicyError(
                f"tool template {template_name!r} is not explicitly allowed by task.allowed_tools"
            )
        template = self._resolve_template(template_name)
        return self._run_template(template_name, template, cwd=cwd)

    def run(self, template_name: str, *, cwd: str | Path) -> ToolRunResult:
        """Execute a trusted template outside the Task-scoped workflow boundary."""
        template = self._resolve_template(template_name)
        return self._run_template(template_name, template, cwd=cwd)
