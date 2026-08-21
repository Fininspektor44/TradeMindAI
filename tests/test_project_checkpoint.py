"""Isolated tests for the authoritative TradeMind project checkpoint system.

Every Git remote in this file is a temporary local bare repository. No test
can address, authenticate to, or push the real project remote.
"""

from __future__ import annotations

import json
import hashlib
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trademind.project_checkpoint import (
    MANIFEST_BEGIN,
    CheckpointError,
    CheckpointMetadata,
    _manifest_from_tag,
    _tag_message,
    build_checkpoint_manifest,
    checkpoint_tag,
    create_checkpoint,
    list_checkpoints,
    verify_checkpoint,
)
from trademind.signal_statistics_provenance import canonical_json_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
CREATE_CLI = REPO_ROOT / "scripts" / "create_trademind_checkpoint.py"
VERIFY_CLI = REPO_ROOT / "scripts" / "verify_trademind_checkpoint.py"
WINDOWS_WRAPPER = REPO_ROOT / "scripts" / "create_trademind_checkpoint.ps1"
CHECKPOINT_DOC = REPO_ROOT / "docs" / "TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed


@dataclass(frozen=True)
class _RepoCase:
    work: Path
    remote: Path


def _repo_case(tmp_path: Path) -> _RepoCase:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare")
    _git(work, "init", "--initial-branch=main")
    _git(work, "config", "user.name", "Checkpoint Test")
    _git(work, "config", "user.email", "checkpoint-test@example.invalid")
    _git(work, "remote", "add", "origin", str(remote))
    (work / "config").mkdir()
    (work / "config" / "critical.json").write_text('{"risk":1}\n', encoding="utf-8")
    (work / "artifact.txt").write_text("accepted artifact\n", encoding="utf-8")
    (work / "README.md").write_text("temporary checkpoint test repository\n", encoding="utf-8")
    (work / ".gitignore").write_text("data/\n", encoding="utf-8")
    _git(work, "add", ".gitignore", "README.md", "artifact.txt", "config/critical.json")
    _git(work, "commit", "-m", "accepted source")
    return _RepoCase(work=work, remote=remote)


def _metadata(**overrides: object) -> CheckpointMetadata:
    values: dict[str, object] = {
        "layer_name": "Safety Layer V1",
        "checkpoint_id": "safety-layer-v1",
        "final_status": "PASS",
        "layer_status": "CLOSED",
        "full_pytest_status": "PASS",
        "full_pytest_summary": "42 passed",
        "ea_version": "1.6",
        "runtime_versions": (("python", "3.12"),),
        "active_hypothesis_ids": ("hypothesis-safe-1",),
        "protected_holdout_ids": ("sha256:" + "a" * 64,),
        "accepted_research_ids": ("research-1",),
        "demo_account_allowlist": ("67206924",),
        "magic_number": 990244,
        "task_names": ("TradeMindAI-Test-Task",),
        "config_paths": ("config/critical.json",),
        "artifact_paths": ("artifact.txt",),
        "notes": ("accepted by isolated test",),
    }
    values.update(overrides)
    return CheckpointMetadata(**values)


def _source_sha(case: _RepoCase) -> str:
    return _git(case.work, "rev-parse", "HEAD").stdout.strip()


def _create(case: _RepoCase, **kwargs: object) -> dict[str, object]:
    return create_checkpoint(repo=case.work, metadata=_metadata(), **kwargs)


def _replace_tag(case: _RepoCase, tag: str, target: str, message: str) -> None:
    _git(case.work, "tag", "--delete", tag)
    message_path = case.work.parent / "replacement-tag-message.txt"
    message_path.write_text(message, encoding="utf-8")
    _git(case.work, "tag", "--annotate", tag, target, "--file", str(message_path))


def test_pass_closed_creates_annotated_remote_verified_checkpoint(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    source_sha = _source_sha(case)
    result = _create(case)

    assert result["checkpoint_status"] == "CREATED"
    assert result["verification_status"] == "VERIFIED"
    assert result["source_commit_sha"] == source_sha
    assert result["checkpoint_commit_sha"] is None
    assert result["tag_target_sha"] == source_sha
    assert result["remote_tag_target_sha"] == source_sha
    assert result["remote_push_verified"] is True
    assert _git(case.work, "cat-file", "-t", "refs/tags/checkpoint/safety-layer-v1").stdout.strip() == "tag"
    assert _git(case.work, "status", "--porcelain").stdout == ""

    _, manifest = _manifest_from_tag(case.work, "checkpoint/safety-layer-v1")
    required = {
        "schema_version", "checkpoint_id", "checkpoint_tag", "created_at_utc",
        "layer_name", "final_status", "layer_status", "source_branch",
        "source_commit_sha", "source_commit_message", "checkpoint_commit_sha",
        "repository_remote", "repository_dirty_before_checkpoint", "full_pytest_status",
        "full_pytest_summary", "critical_versions", "research_identity",
        "execution_identity", "config_fingerprints", "important_artifact_fingerprints",
        "notes", "manifest_sha256",
    }
    assert required == set(manifest)
    assert manifest["repository_dirty_before_checkpoint"] is False
    assert verify_checkpoint(case.work, "safety-layer-v1")["verification_status"] == "VERIFIED"


@pytest.mark.parametrize(
    ("final_status", "layer_status", "error_code"),
    [
        ("FAIL", "CLOSED", "FINAL_STATUS_NOT_PASS"),
        ("PASS", "NOT CLOSED", "LAYER_STATUS_NOT_CLOSED"),
    ],
)
def test_non_pass_or_non_closed_is_rejected(
    tmp_path: Path, final_status: str, layer_status: str, error_code: str
) -> None:
    case = _repo_case(tmp_path)
    with pytest.raises(CheckpointError) as caught:
        create_checkpoint(
            repo=case.work,
            metadata=_metadata(final_status=final_status, layer_status=layer_status),
        )
    assert caught.value.code == error_code
    assert _git(case.work, "tag", "--list").stdout == ""
    assert _git(case.remote, "show-ref", check=False).stdout == ""


def test_dirty_repo_rejected_and_unrelated_file_is_never_added(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    unrelated = case.work / "unrelated.txt"
    unrelated.write_text("operator-owned\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="modified or untracked") as caught:
        _create(case)
    assert caught.value.code == "DIRTY_REPOSITORY"
    assert unrelated.is_file()
    assert _git(case.work, "ls-files", "--error-unmatch", "unrelated.txt", check=False).returncode != 0
    assert _git(case.work, "tag", "--list").stdout == ""


def test_merge_conflict_is_rejected(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    _git(case.work, "switch", "-c", "conflicting")
    (case.work / "README.md").write_text("branch version\n", encoding="utf-8")
    _git(case.work, "add", "README.md")
    _git(case.work, "commit", "-m", "branch change")
    _git(case.work, "switch", "main")
    (case.work / "README.md").write_text("main version\n", encoding="utf-8")
    _git(case.work, "add", "README.md")
    _git(case.work, "commit", "-m", "main change")
    assert _git(case.work, "merge", "conflicting", check=False).returncode != 0

    with pytest.raises(CheckpointError) as caught:
        _create(case)
    assert caught.value.code in {"GIT_OPERATION_IN_PROGRESS", "MERGE_CONFLICT"}
    assert _git(case.work, "tag", "--list").stdout == ""


def test_existing_local_tag_is_never_overwritten(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    tag = checkpoint_tag("safety-layer-v1")
    _git(case.work, "tag", "--annotate", tag, "-m", "preexisting")
    before = _git(case.work, "rev-parse", f"refs/tags/{tag}").stdout.strip()
    with pytest.raises(CheckpointError) as caught:
        _create(case)
    assert caught.value.code == "LOCAL_TAG_EXISTS"
    assert _git(case.work, "rev-parse", f"refs/tags/{tag}").stdout.strip() == before


def test_conflicting_remote_tag_is_never_overwritten(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    tag_ref = "refs/tags/checkpoint/safety-layer-v1"
    _git(case.work, "push", "origin", "HEAD:refs/heads/main")
    subprocess.run(
        ["git", "--git-dir", str(case.remote), "update-ref", tag_ref, _source_sha(case)],
        check=True,
    )
    before = subprocess.run(
        ["git", "--git-dir", str(case.remote), "rev-parse", tag_ref],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    with pytest.raises(CheckpointError) as caught:
        _create(case)
    assert caught.value.code == "REMOTE_TAG_EXISTS"
    after = subprocess.run(
        ["git", "--git-dir", str(case.remote), "rev-parse", tag_ref],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    assert after == before


def test_push_failure_never_reports_success(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    hook = case.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    failed = subprocess.run(
        [
            sys.executable, str(CREATE_CLI), "--repo", str(case.work),
            "--layer-name", "Safety Layer V1", "--checkpoint-id", "safety-layer-v1",
            "--final-status", "PASS", "--layer-status", "CLOSED",
        ],
        text=True, capture_output=True, check=False,
    )
    assert failed.returncode == 1
    payload = json.loads(failed.stdout)
    assert payload["checkpoint_status"] == "NOT_CREATED"
    assert payload["error_code"] == "PUSH_FAILED"
    assert _git(case.remote, "show-ref", check=False).stdout == ""
    # Local diagnosis metadata may remain, but it was not called CREATED.
    assert _git(case.work, "tag", "--list").stdout.strip() == "checkpoint/safety-layer-v1"


def test_source_commit_is_exact_and_no_metadata_commit_can_hide_it(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    source_sha = _source_sha(case)
    result = _create(case)
    assert _source_sha(case) == source_sha
    assert result["source_commit_sha"] == source_sha
    assert result["checkpoint_commit_sha"] is None
    assert _git(case.work, "rev-parse", "checkpoint/safety-layer-v1^{}").stdout.strip() == source_sha


def test_manifest_sha_is_deterministic_for_identical_proven_inputs(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    state = {
        "source_branch": "main",
        "source_commit_sha": _source_sha(case),
        "source_commit_message": "accepted source",
        "repository_remote": str(case.remote),
    }
    fixed = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    first = build_checkpoint_manifest(
        repo=case.work, metadata=_metadata(), repository_state=state, created_at=fixed
    )
    second = build_checkpoint_manifest(
        repo=case.work, metadata=_metadata(), repository_state=state, created_at=fixed
    )
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["manifest_sha256"] == second["manifest_sha256"]


def test_tampered_embedded_manifest_fails_verifier(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    _create(case)
    tag = "checkpoint/safety-layer-v1"
    message = _git(case.work, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}").stdout
    assert MANIFEST_BEGIN in message
    tampered = message.replace("accepted by isolated test", "tampered after creation")
    _replace_tag(case, tag, _source_sha(case), tampered)
    with pytest.raises(CheckpointError) as caught:
        verify_checkpoint(case.work, tag)
    assert caught.value.code == "MANIFEST_HASH_MISMATCH"


def test_wrong_tag_target_fails_verifier(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    _create(case)
    tag = "checkpoint/safety-layer-v1"
    message = _git(case.work, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}").stdout
    (case.work / "later.txt").write_text("later\n", encoding="utf-8")
    _git(case.work, "add", "later.txt")
    _git(case.work, "commit", "-m", "later commit")
    _replace_tag(case, tag, _source_sha(case), message)
    with pytest.raises(CheckpointError) as caught:
        verify_checkpoint(case.work, tag)
    assert caught.value.code == "TAG_TARGET_MISMATCH"


def test_changed_fingerprint_in_manifest_fails_verifier(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    _create(case)
    tag = "checkpoint/safety-layer-v1"
    _, manifest = _manifest_from_tag(case.work, tag)
    manifest["config_fingerprints"][0]["sha256"] = "0" * 64
    semantic = dict(manifest)
    semantic.pop("manifest_sha256")
    manifest["manifest_sha256"] = hashlib.sha256(
        canonical_json_bytes(semantic)
    ).hexdigest()
    _replace_tag(case, tag, _source_sha(case), _tag_message(manifest))
    with pytest.raises(CheckpointError) as caught:
        verify_checkpoint(case.work, tag)
    assert caught.value.code == "FINGERPRINT_MISMATCH"


def test_local_git_bundle_verifies_and_restores_without_remote(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    result = _create(case, create_bundle=True)
    bundle = result["local_recovery_bundle"]
    assert bundle is not None
    bundle_dir = Path(bundle["directory"])
    proof = verify_checkpoint(case.work, "safety-layer-v1", bundle_dir=bundle_dir)
    assert proof["local_recovery_bundle"]["bundle_sha256"] == bundle["bundle_sha256"]

    restored = tmp_path / "restored"
    _git(tmp_path, "clone", str(bundle_dir / "checkpoint.bundle"), str(restored))
    assert _git(restored, "cat-file", "-e", f"{result['source_commit_sha']}^{{commit}}", check=False).returncode == 0


def test_in_repository_bundle_root_must_be_gitignored_before_tag_creation(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    with pytest.raises(CheckpointError) as caught:
        _create(case, create_bundle=True, bundle_root=Path("recovery-artifacts"))
    assert caught.value.code == "BUNDLE_ROOT_NOT_IGNORED"
    assert _git(case.work, "tag", "--list").stdout == ""
    assert not (case.work / "recovery-artifacts").exists()


def test_environment_is_never_dumped_into_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _repo_case(tmp_path)
    monkeypatch.setenv("SER8_HOLDOUT_KEY", "never-persist-this-value")
    _create(case)
    message = _git(
        case.work, "for-each-ref", "--format=%(contents)",
        "refs/tags/checkpoint/safety-layer-v1",
    ).stdout
    assert "SER8_HOLDOUT_KEY" not in message
    assert "never-persist-this-value" not in message


@pytest.mark.parametrize(
    "secret_note",
    ["SER8_HOLDOUT_KEY=abc", "github token: ghp_abcdefghijklmnopqrstuvwxyz", "password=secret"],
)
def test_secret_like_metadata_attempt_is_rejected(tmp_path: Path, secret_note: str) -> None:
    case = _repo_case(tmp_path)
    with pytest.raises(CheckpointError) as caught:
        create_checkpoint(repo=case.work, metadata=_metadata(notes=(secret_note,)))
    assert caught.value.code == "SECRET_METADATA_REJECTED"
    assert _git(case.work, "tag", "--list").stdout == ""


def test_checkpoint_listing_reports_verified_metadata(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    _create(case)
    listing = list_checkpoints(case.work)
    assert listing["checkpoint_count"] == 1
    assert listing["checkpoints"] == [
        {
            "checkpoint_id": "safety-layer-v1",
            "created_at_utc": listing["checkpoints"][0]["created_at_utc"],
            "layer_name": "Safety Layer V1",
            "source_commit_sha": _source_sha(case),
            "checkpoint_tag": "checkpoint/safety-layer-v1",
            "verification_status": "VERIFIED",
        }
    ]


def test_python_create_and_verify_clis_work_with_local_remote(tmp_path: Path) -> None:
    case = _repo_case(tmp_path)
    created = subprocess.run(
        [
            sys.executable, str(CREATE_CLI), "--repo", str(case.work),
            "--layer-name", "CLI Safety Layer", "--checkpoint-id", "cli-safety-layer",
            "--final-status", "PASS", "--layer-status", "CLOSED",
            "--full-pytest-status", "PASS", "--config", "config/critical.json",
        ],
        text=True, capture_output=True, check=False,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    assert json.loads(created.stdout)["checkpoint_status"] == "CREATED"
    verified = subprocess.run(
        [
            sys.executable, str(VERIFY_CLI), "--repo", str(case.work),
            "--checkpoint", "checkpoint/cli-safety-layer",
        ],
        text=True, capture_output=True, check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["verification_status"] == "VERIFIED"


def test_windows_wrapper_preserves_argument_array_and_quoting() -> None:
    source = WINDOWS_WRAPPER.read_text(encoding="utf-8")
    assert '& $python $script @args' in source
    assert "Start-Process" not in source
    assert "Invoke-Expression" not in source
    assert "$args -join" not in source
    assert source.count("{") == source.count("}")
    assert source.count("(") == source.count(")")


def test_safe_recovery_documentation_never_defaults_to_hard_reset() -> None:
    text = CHECKPOINT_DOC.read_text(encoding="utf-8")
    assert "git switch -c recovery/" in text
    assert "git fetch origin" in text
    assert "git clone data/project_checkpoints/" in text
    assert "git show --stat" in text
    assert "git reset --hard" not in text


def test_checkpoint_implementation_contains_no_force_push_or_environment_dump() -> None:
    source = (REPO_ROOT / "src" / "trademind" / "project_checkpoint.py").read_text(
        encoding="utf-8"
    )
    assert '"--force"' not in source
    assert "force-with-lease" not in source
    assert "os.environ" not in source
    assert "environ.copy" not in source
    assert "git add" not in source
