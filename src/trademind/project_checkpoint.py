"""Authoritative TradeMind project checkpoint creation and verification.

The checkpoint tag points directly to the accepted source commit.  Its
annotated message embeds the canonical checkpoint manifest, avoiding a
self-referential metadata commit while making the manifest durable in Git.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from trademind.signal_statistics_provenance import canonical_json_bytes

MANIFEST_SCHEMA_VERSION = "trademind-project-checkpoint-manifest-v1"
RESULT_SCHEMA_VERSION = "trademind-project-checkpoint-result-v1"
TAG_PREFIX = "checkpoint/"
MANIFEST_BEGIN = "-----BEGIN TRADEMIND CHECKPOINT MANIFEST-----"
MANIFEST_END = "-----END TRADEMIND CHECKPOINT MANIFEST-----"
DEFAULT_BUNDLE_ROOT = Path("data/project_checkpoints")

_SAFE_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")
_SECRET_TEXT = re.compile(
    r"(?i)(SER8_HOLDOUT_KEY|github[_ -]?token|api[_ -]?key|broker[_ -]?password|"
    r"password\s*[:=]|private[_ -]?key|terminal[_ -]?credential|credential\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,})"
)
_SECRET_PATH_PARTS = {
    ".env", "secrets", "credentials", "id_rsa", "id_ed25519", "private_key", "private-key"
}
_REQUIRED_MANIFEST_FIELDS = {
    "schema_version", "checkpoint_id", "checkpoint_tag", "created_at_utc",
    "layer_name", "final_status", "layer_status", "source_branch",
    "source_commit_sha", "source_commit_message", "checkpoint_commit_sha",
    "repository_remote", "repository_dirty_before_checkpoint", "full_pytest_status",
    "full_pytest_summary", "critical_versions", "research_identity",
    "execution_identity", "config_fingerprints", "important_artifact_fingerprints",
    "notes", "manifest_sha256",
}


class CheckpointError(RuntimeError):
    """A fail-closed checkpoint or verification error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CheckpointMetadata:
    layer_name: str
    final_status: str
    layer_status: str
    checkpoint_id: str | None = None
    full_pytest_status: str | None = None
    full_pytest_summary: str | None = None
    ea_version: str | None = None
    runtime_versions: tuple[tuple[str, str], ...] = ()
    active_hypothesis_ids: tuple[str, ...] = ()
    protected_holdout_ids: tuple[str, ...] = ()
    accepted_research_ids: tuple[str, ...] = ()
    demo_account_allowlist: tuple[str, ...] = ()
    magic_number: int | None = None
    task_names: tuple[str, ...] = ()
    config_paths: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CheckpointError("INVALID_TIMESTAMP", "checkpoint timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_checkpoint_id(value: str) -> str:
    """Return a deterministic lowercase slug suitable for ``checkpoint/<id>``."""
    text = value.strip().lower()
    if text.startswith(TAG_PREFIX):
        text = text[len(TAG_PREFIX):]
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text or len(text) > 120:
        raise CheckpointError("INVALID_CHECKPOINT_ID", "checkpoint id is empty or longer than 120 characters")
    return text


def checkpoint_tag(checkpoint_id: str) -> str:
    return TAG_PREFIX + normalize_checkpoint_id(checkpoint_id)


def _run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise CheckpointError("GIT_COMMAND_FAILED", f"git {args[0]} failed{suffix}")
    return completed


def _run_git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise CheckpointError("GIT_COMMAND_FAILED", f"git {args[0]} failed{suffix}")
    return completed.stdout


def _repo_root(repo: Path) -> Path:
    candidate = repo.expanduser().resolve()
    result = _run_git(candidate, "rev-parse", "--show-toplevel")
    actual = Path(result.stdout.strip()).resolve()
    if actual != candidate:
        raise CheckpointError(
            "WRONG_REPOSITORY_ROOT",
            f"--repo must be the Git top-level directory ({actual})",
        )
    return actual


def _git_path(repo: Path, name: str) -> Path:
    raw = _run_git(repo, "rev-parse", "--git-path", name).stdout.strip()
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _assert_safe_text(value: str | None, *, field_name: str) -> None:
    if value is None:
        return
    if _SECRET_TEXT.search(value):
        raise CheckpointError(
            "SECRET_METADATA_REJECTED",
            f"{field_name} looks secret-like and cannot be checkpointed",
        )


def _assert_safe_metadata(metadata: CheckpointMetadata) -> None:
    values: list[tuple[str, str | None]] = [
        ("layer_name", metadata.layer_name),
        ("checkpoint_id", metadata.checkpoint_id),
        ("full_pytest_summary", metadata.full_pytest_summary),
        ("ea_version", metadata.ea_version),
    ]
    values.extend(("runtime version key", key) for key, _ in metadata.runtime_versions)
    values.extend(("runtime version value", value) for _, value in metadata.runtime_versions)
    values.extend(("active_hypothesis_id", value) for value in metadata.active_hypothesis_ids)
    values.extend(("protected_holdout_id", value) for value in metadata.protected_holdout_ids)
    values.extend(("accepted_research_id", value) for value in metadata.accepted_research_ids)
    values.extend(("demo_account_allowlist", value) for value in metadata.demo_account_allowlist)
    values.extend(("task_name", value) for value in metadata.task_names)
    values.extend(("note", value) for value in metadata.notes)
    for field_name, value in values:
        _assert_safe_text(value, field_name=field_name)
    for key, _ in metadata.runtime_versions:
        if not _SAFE_KEY.fullmatch(key):
            raise CheckpointError("INVALID_METADATA_KEY", f"invalid runtime version key: {key!r}")


def _safe_remote_url(url: str) -> str:
    value = url.strip()
    if not value:
        raise CheckpointError("REMOTE_MISSING", "repository remote URL is empty")
    if re.match(r"(?i)^https?://[^/]*@", value):
        raise CheckpointError(
            "SECRET_REMOTE_REJECTED",
            "credential-bearing HTTP(S) remote URLs cannot be persisted",
        )
    _assert_safe_text(value, field_name="repository_remote")
    return value


def _assert_repository_integrity(repo: Path, *, remote: str) -> dict[str, str]:
    source_sha = _run_git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    branch_result = _run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch_result.returncode != 0 or not branch_result.stdout.strip():
        raise CheckpointError("DETACHED_HEAD", "current branch is unknown or HEAD is detached")
    branch = branch_result.stdout.strip()

    unresolved_paths = (
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"
    )
    present = [name for name in unresolved_paths if _git_path(repo, name).exists()]
    if present:
        raise CheckpointError(
            "GIT_OPERATION_IN_PROGRESS",
            f"unresolved Git operation state exists: {', '.join(present)}",
        )
    if _run_git(repo, "ls-files", "-u").stdout.strip():
        raise CheckpointError("MERGE_CONFLICT", "repository contains unresolved merge conflicts")
    status = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status.strip():
        raise CheckpointError("DIRTY_REPOSITORY", "repository has modified or untracked files")

    reachable = _run_git(
        repo,
        "merge-base",
        "--is-ancestor",
        source_sha,
        f"refs/heads/{branch}",
        check=False,
    )
    if reachable.returncode != 0:
        raise CheckpointError("SOURCE_NOT_REACHABLE", "HEAD is not reachable from the current branch")

    remotes = set(_run_git(repo, "remote").stdout.split())
    if remote not in remotes:
        raise CheckpointError("REMOTE_MISSING", f"required remote {remote!r} does not exist")
    remote_url = _safe_remote_url(_run_git(repo, "remote", "get-url", remote).stdout)
    message = _run_git(repo, "show", "-s", "--format=%s", source_sha).stdout.rstrip("\n")
    return {
        "source_commit_sha": source_sha,
        "source_branch": branch,
        "source_commit_message": message,
        "repository_remote": remote_url,
    }


def _assert_tag_absent(repo: Path, *, remote: str, tag: str) -> None:
    local = _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}", check=False)
    if local.returncode == 0:
        raise CheckpointError("LOCAL_TAG_EXISTS", f"local checkpoint tag already exists: {tag}")
    remote_result = _run_git(repo, "ls-remote", "--tags", remote, f"refs/tags/{tag}", check=False)
    if remote_result.returncode != 0:
        raise CheckpointError("REMOTE_UNREACHABLE", "cannot inspect remote checkpoint tags")
    if remote_result.stdout.strip():
        raise CheckpointError("REMOTE_TAG_EXISTS", f"remote checkpoint tag already exists: {tag}")


def _safe_repo_relative_path(repo: Path, supplied: str, *, field_name: str) -> str:
    path = Path(supplied)
    absolute = path.expanduser().resolve() if path.is_absolute() else (repo / path).resolve()
    try:
        relative = absolute.relative_to(repo)
    except ValueError as exc:
        raise CheckpointError(
            "FINGERPRINT_PATH_OUTSIDE_REPOSITORY",
            f"{field_name} path must be inside the repository: {supplied}",
        ) from exc
    if not relative.parts or relative.parts[0] == ".git":
        raise CheckpointError("INVALID_FINGERPRINT_PATH", f"invalid {field_name} path: {supplied}")
    lowered_parts = {part.lower() for part in relative.parts}
    secret_like_path = lowered_parts & _SECRET_PATH_PARTS or any(
        part.lower().startswith(".env")
        or "secret" in part.lower()
        or "credential" in part.lower()
        or part.lower().endswith((".pem", ".key"))
        for part in relative.parts
    )
    if secret_like_path:
        raise CheckpointError(
            "SECRET_PATH_REJECTED",
            f"secret-like {field_name} path cannot be fingerprinted: {relative.as_posix()}",
        )
    return relative.as_posix()


def _fingerprints_at_source(
    repo: Path,
    source_sha: str,
    paths: Sequence[str],
    *,
    field_name: str,
) -> list[dict[str, str]]:
    seen: set[str] = set()
    records: list[dict[str, str]] = []
    for supplied in paths:
        relative = _safe_repo_relative_path(repo, supplied, field_name=field_name)
        if relative in seen:
            raise CheckpointError("DUPLICATE_FINGERPRINT_PATH", f"duplicate path: {relative}")
        seen.add(relative)
        exists = _run_git(repo, "cat-file", "-e", f"{source_sha}:{relative}", check=False)
        if exists.returncode != 0:
            raise CheckpointError(
                "UNTRACKED_FINGERPRINT_PATH",
                f"{field_name} path is not tracked at the source commit: {relative}",
            )
        content = _run_git_bytes(repo, "show", f"{source_sha}:{relative}")
        records.append({"path": relative, "sha256": _sha256(content)})
    return records


def build_checkpoint_manifest(
    *,
    repo: Path,
    metadata: CheckpointMetadata,
    repository_state: Mapping[str, str],
    created_at: datetime,
) -> dict[str, object]:
    """Build a canonical, hash-bearing manifest without mutating Git."""
    if metadata.final_status != "PASS":
        raise CheckpointError("FINAL_STATUS_NOT_PASS", "FINAL STATUS must be exactly PASS")
    if metadata.layer_status != "CLOSED":
        raise CheckpointError("LAYER_STATUS_NOT_CLOSED", "LAYER STATUS must be exactly CLOSED")
    if metadata.full_pytest_status not in {None, "PASS"}:
        raise CheckpointError("FULL_PYTEST_NOT_PASS", "supplied full pytest status must be PASS")
    if not metadata.layer_name.strip():
        raise CheckpointError("LAYER_NAME_MISSING", "layer name must be non-empty")
    _assert_safe_metadata(metadata)

    checkpoint_id = normalize_checkpoint_id(metadata.checkpoint_id or metadata.layer_name)
    tag = checkpoint_tag(checkpoint_id)
    source_sha = repository_state["source_commit_sha"]
    runtime_versions = dict(metadata.runtime_versions)
    config_fingerprints = _fingerprints_at_source(
        repo, source_sha, metadata.config_paths, field_name="config"
    )
    artifact_fingerprints = _fingerprints_at_source(
        repo, source_sha, metadata.artifact_paths, field_name="artifact"
    )
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "checkpoint_tag": tag,
        "created_at_utc": _utc_text(created_at),
        "layer_name": metadata.layer_name.strip(),
        "final_status": metadata.final_status,
        "layer_status": metadata.layer_status,
        "source_branch": repository_state["source_branch"],
        "source_commit_sha": source_sha,
        "source_commit_message": repository_state["source_commit_message"],
        # No metadata commit is created: the annotated tag embeds this manifest
        # and points directly to source_commit_sha, avoiding circular identity.
        "checkpoint_commit_sha": None,
        "repository_remote": repository_state["repository_remote"],
        "repository_dirty_before_checkpoint": False,
        "full_pytest_status": metadata.full_pytest_status,
        "full_pytest_summary": metadata.full_pytest_summary,
        "critical_versions": {
            "ea_version": metadata.ea_version,
            "runtime_versions": runtime_versions,
        },
        "research_identity": {
            "active_hypothesis_ids": list(metadata.active_hypothesis_ids),
            "protected_holdout_identifiers": list(metadata.protected_holdout_ids),
            "accepted_research_ids": list(metadata.accepted_research_ids),
        },
        "execution_identity": {
            "demo_account_allowlist": list(metadata.demo_account_allowlist),
            "magic_number": metadata.magic_number,
            "task_names": list(metadata.task_names),
        },
        "config_fingerprints": config_fingerprints,
        "important_artifact_fingerprints": artifact_fingerprints,
        "notes": list(metadata.notes),
    }
    manifest["manifest_sha256"] = _sha256(canonical_json_bytes(manifest))
    return manifest


def verify_manifest_hash(manifest: Mapping[str, object]) -> str:
    supplied = manifest.get("manifest_sha256")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise CheckpointError("MANIFEST_HASH_MISSING", "manifest_sha256 is absent or malformed")
    semantic = dict(manifest)
    del semantic["manifest_sha256"]
    computed = _sha256(canonical_json_bytes(semantic))
    if computed != supplied:
        raise CheckpointError("MANIFEST_HASH_MISMATCH", "checkpoint manifest SHA-256 does not match")
    return computed


def _tag_message(manifest: Mapping[str, object]) -> str:
    manifest_json = canonical_json_bytes(manifest).decode("utf-8")
    return "\n".join(
        (
            "TRADEMIND PROJECT CHECKPOINT V1",
            f"checkpoint_id: {manifest['checkpoint_id']}",
            f"source_commit_sha: {manifest['source_commit_sha']}",
            f"manifest_sha256: {manifest['manifest_sha256']}",
            f"layer_name: {manifest['layer_name']}",
            "final_status: PASS",
            "layer_status: CLOSED",
            MANIFEST_BEGIN,
            manifest_json,
            MANIFEST_END,
            "",
        )
    )


def _parse_tag_message(message: str) -> tuple[dict[str, str], dict[str, object]]:
    if MANIFEST_BEGIN not in message or MANIFEST_END not in message:
        raise CheckpointError("TAG_MANIFEST_MISSING", "annotated tag does not embed a checkpoint manifest")
    header_text, remainder = message.split(MANIFEST_BEGIN, 1)
    header_lines = header_text.splitlines()
    if not header_lines or header_lines[0] != "TRADEMIND PROJECT CHECKPOINT V1":
        raise CheckpointError("TAG_MESSAGE_MALFORMED", "checkpoint tag title is missing or invalid")
    manifest_text, trailing = remainder.split(MANIFEST_END, 1)
    if trailing.strip():
        raise CheckpointError("TAG_MESSAGE_MALFORMED", "unexpected data follows the embedded manifest")
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise CheckpointError("TAG_MESSAGE_MALFORMED", f"malformed tag header line: {line!r}")
        headers[key.strip()] = value.strip()
    raw = manifest_text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointError("TAG_MANIFEST_INVALID_JSON", "embedded checkpoint manifest is invalid JSON") from exc
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed).decode("utf-8") != raw:
        raise CheckpointError("TAG_MANIFEST_NONCANONICAL", "embedded checkpoint manifest is not canonical JSON")
    return headers, parsed


def _create_annotated_tag(repo: Path, manifest: Mapping[str, object]) -> None:
    with tempfile.TemporaryDirectory(prefix="trademind-checkpoint-") as temporary:
        message_path = Path(temporary) / "tag-message.txt"
        message_path.write_text(_tag_message(manifest), encoding="utf-8")
        _run_git(
            repo,
            "tag",
            "--annotate",
            str(manifest["checkpoint_tag"]),
            str(manifest["source_commit_sha"]),
            "--file",
            str(message_path),
        )


def _validated_bundle_root(repo: Path, bundle_root: Path) -> Path:
    root = (bundle_root if bundle_root.is_absolute() else repo / bundle_root).resolve()
    try:
        relative_root = root.relative_to(repo)
    except ValueError:
        relative_root = None
    if relative_root is not None:
        ignored = _run_git(
            repo,
            "check-ignore",
            "--quiet",
            "--no-index",
            relative_root.as_posix(),
            check=False,
        )
        if ignored.returncode != 0:
            raise CheckpointError(
                "BUNDLE_ROOT_NOT_IGNORED",
                "an in-repository recovery bundle root must be covered by .gitignore",
            )
    return root


def _write_recovery_bundle(
    repo: Path,
    manifest: Mapping[str, object],
    *,
    bundle_root: Path,
) -> dict[str, object]:
    root = _validated_bundle_root(repo, bundle_root)
    destination = root / str(manifest["checkpoint_id"])
    if destination.exists():
        raise CheckpointError("BUNDLE_EXISTS", f"recovery bundle directory already exists: {destination}")
    destination.mkdir(parents=True)
    manifest_path = destination / "checkpoint_manifest.json"
    bundle_path = destination / "checkpoint.bundle"
    sums_path = destination / "SHA256SUMS"
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    manifest_path.write_bytes(manifest_bytes)
    _run_git(
        repo,
        "bundle",
        "create",
        str(bundle_path),
        f"refs/heads/{manifest['source_branch']}",
        f"refs/tags/{manifest['checkpoint_tag']}",
    )
    _run_git(repo, "bundle", "verify", str(bundle_path))
    sums = {
        manifest_path.name: _sha256(manifest_path.read_bytes()),
        bundle_path.name: _sha256(bundle_path.read_bytes()),
    }
    sums_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="ascii",
    )
    return {
        "directory": str(destination),
        "manifest_path": str(manifest_path),
        "bundle_path": str(bundle_path),
        "sha256sums_path": str(sums_path),
        "bundle_sha256": sums[bundle_path.name],
    }


def _remote_refs(repo: Path, remote: str, branch: str, tag: str) -> dict[str, str]:
    result = _run_git(
        repo,
        "ls-remote",
        remote,
        f"refs/heads/{branch}",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
        check=False,
    )
    if result.returncode != 0:
        raise CheckpointError("REMOTE_VERIFY_FAILED", "remote refs could not be read after push")
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, separator, ref = line.partition("\t")
        if separator:
            refs[ref] = sha
    return refs


def _push_and_verify(repo: Path, manifest: Mapping[str, object], *, remote: str) -> dict[str, str]:
    branch = str(manifest["source_branch"])
    tag = str(manifest["checkpoint_tag"])
    source_sha = str(manifest["source_commit_sha"])
    push = _run_git(
        repo,
        "push",
        "--atomic",
        "--porcelain",
        remote,
        f"HEAD:refs/heads/{branch}",
        f"refs/tags/{tag}:refs/tags/{tag}",
        check=False,
    )
    if push.returncode != 0:
        raise CheckpointError("PUSH_FAILED", "atomic branch/tag push failed; checkpoint was not created")
    refs = _remote_refs(repo, remote, branch, tag)
    if refs.get(f"refs/heads/{branch}") != source_sha:
        raise CheckpointError("REMOTE_BRANCH_MISMATCH", "remote branch does not point to the source commit")
    if f"refs/tags/{tag}" not in refs:
        raise CheckpointError("REMOTE_TAG_MISSING", "remote checkpoint tag is missing after push")
    if refs.get(f"refs/tags/{tag}^{{}}") != source_sha:
        raise CheckpointError("REMOTE_TAG_TARGET_MISMATCH", "remote annotated tag target is incorrect")
    return {
        "remote_branch_sha": refs[f"refs/heads/{branch}"],
        "remote_tag_object_sha": refs[f"refs/tags/{tag}"],
        "remote_tag_target_sha": refs[f"refs/tags/{tag}^{{}}"],
    }


def create_checkpoint(
    *,
    repo: Path,
    metadata: CheckpointMetadata,
    remote: str = "origin",
    create_bundle: bool = False,
    bundle_root: Path = DEFAULT_BUNDLE_ROOT,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Create, atomically push, and verify one immutable checkpoint."""
    root = _repo_root(repo)
    state = _assert_repository_integrity(root, remote=remote)
    manifest = build_checkpoint_manifest(
        repo=root,
        metadata=metadata,
        repository_state=state,
        created_at=created_at or datetime.now(timezone.utc),
    )
    tag = str(manifest["checkpoint_tag"])
    _assert_tag_absent(root, remote=remote, tag=tag)
    if create_bundle:
        _validated_bundle_root(root, bundle_root)
    _create_annotated_tag(root, manifest)
    bundle = None
    if create_bundle:
        bundle = _write_recovery_bundle(root, manifest, bundle_root=bundle_root)
    remote_proof = _push_and_verify(root, manifest, remote=remote)
    verification = verify_checkpoint(root, tag, bundle_dir=Path(bundle["directory"]) if bundle else None)
    if verification["verification_status"] != "VERIFIED":
        raise CheckpointError("POST_CREATE_VERIFY_FAILED", "checkpoint did not pass local verification")
    tag_object_sha = _run_git(root, "rev-parse", f"refs/tags/{tag}").stdout.strip()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "checkpoint_status": "CREATED",
        "verification_status": "VERIFIED",
        "checkpoint_id": manifest["checkpoint_id"],
        "checkpoint_tag": tag,
        "source_branch": manifest["source_branch"],
        "source_commit_sha": manifest["source_commit_sha"],
        "checkpoint_commit_sha": None,
        "tag_object_sha": tag_object_sha,
        "tag_target_sha": manifest["source_commit_sha"],
        "manifest_sha256": manifest["manifest_sha256"],
        "repository_remote": manifest["repository_remote"],
        "remote_push_verified": True,
        **remote_proof,
        "local_recovery_bundle": bundle,
    }


def _manifest_from_tag(repo: Path, tag: str) -> tuple[dict[str, str], dict[str, object]]:
    ref = f"refs/tags/{tag}"
    exists = _run_git(repo, "show-ref", "--verify", "--quiet", ref, check=False)
    if exists.returncode != 0:
        raise CheckpointError("TAG_NOT_FOUND", f"checkpoint tag does not exist: {tag}")
    object_type = _run_git(repo, "cat-file", "-t", ref).stdout.strip()
    if object_type != "tag":
        raise CheckpointError("TAG_NOT_ANNOTATED", f"checkpoint tag is not annotated: {tag}")
    message = _run_git(repo, "for-each-ref", "--format=%(contents)", ref).stdout
    return _parse_tag_message(message)


def _validate_manifest_structure(manifest: Mapping[str, object]) -> None:
    if set(manifest) != _REQUIRED_MANIFEST_FIELDS:
        raise CheckpointError("MANIFEST_FIELDS_INVALID", "checkpoint manifest fields are incomplete or unknown")
    for key in (
        "checkpoint_id", "checkpoint_tag", "created_at_utc", "layer_name", "final_status",
        "layer_status", "source_branch", "source_commit_sha", "source_commit_message",
        "repository_remote", "manifest_sha256",
    ):
        if not isinstance(manifest.get(key), str) or not str(manifest[key]).strip():
            raise CheckpointError("MANIFEST_FIELD_INVALID", f"manifest field is invalid: {key}")
    if manifest["checkpoint_id"] != normalize_checkpoint_id(str(manifest["checkpoint_id"])):
        raise CheckpointError("MANIFEST_CHECKPOINT_ID_INVALID", "manifest checkpoint_id is not normalized")
    if manifest["checkpoint_tag"] != checkpoint_tag(str(manifest["checkpoint_id"])):
        raise CheckpointError("MANIFEST_TAG_INVALID", "manifest checkpoint_tag is inconsistent")
    created_at = str(manifest["created_at_utc"])
    if not created_at.endswith("Z"):
        raise CheckpointError("MANIFEST_TIMESTAMP_INVALID", "created_at_utc must use UTC Z notation")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckpointError("MANIFEST_TIMESTAMP_INVALID", "created_at_utc is not an ISO timestamp") from exc
    if manifest["repository_dirty_before_checkpoint"] is not False:
        raise CheckpointError("MANIFEST_DIRTY_STATE_INVALID", "checkpoint manifest must prove a clean source")
    if manifest["checkpoint_commit_sha"] is not None:
        raise CheckpointError("UNEXPECTED_CHECKPOINT_COMMIT", "V1 checkpoint tags must point directly to source")
    if manifest["full_pytest_status"] not in {None, "PASS"}:
        raise CheckpointError("MANIFEST_PYTEST_STATUS_INVALID", "full_pytest_status must be PASS or null")
    if manifest["full_pytest_summary"] is not None and not isinstance(
        manifest["full_pytest_summary"], str
    ):
        raise CheckpointError("MANIFEST_PYTEST_SUMMARY_INVALID", "full_pytest_summary must be text or null")
    for key in ("critical_versions", "research_identity", "execution_identity"):
        if not isinstance(manifest[key], dict):
            raise CheckpointError("MANIFEST_FIELD_INVALID", f"manifest field must be an object: {key}")
    for key in ("config_fingerprints", "important_artifact_fingerprints", "notes"):
        if not isinstance(manifest[key], list):
            raise CheckpointError("MANIFEST_FIELD_INVALID", f"manifest field must be a list: {key}")
    if not all(isinstance(note, str) for note in manifest["notes"]):
        raise CheckpointError("MANIFEST_NOTES_INVALID", "notes must contain only text")
    for note in manifest["notes"]:
        _assert_safe_text(note, field_name="note")
    _safe_remote_url(str(manifest["repository_remote"]))


def _verify_fingerprints(repo: Path, manifest: Mapping[str, object], key: str) -> None:
    records = manifest.get(key)
    if not isinstance(records, list):
        raise CheckpointError("FINGERPRINTS_MALFORMED", f"{key} must be a list")
    source_sha = str(manifest["source_commit_sha"])
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise CheckpointError("FINGERPRINT_MALFORMED", f"invalid {key} entry")
        path = record["path"]
        digest = record["sha256"]
        if not isinstance(path, str) or not isinstance(digest, str):
            raise CheckpointError("FINGERPRINT_MALFORMED", f"invalid {key} types")
        normalized_path = _safe_repo_relative_path(repo, path, field_name=key)
        if normalized_path != path:
            raise CheckpointError("FINGERPRINT_PATH_INVALID", f"noncanonical fingerprint path: {path}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CheckpointError("FINGERPRINT_MALFORMED", f"invalid SHA-256 for {path}")
        exists = _run_git(repo, "cat-file", "-e", f"{source_sha}:{path}", check=False)
        if exists.returncode != 0:
            raise CheckpointError("FINGERPRINT_SOURCE_MISSING", f"source file is missing: {path}")
        actual = _sha256(_run_git_bytes(repo, "show", f"{source_sha}:{path}"))
        if actual != digest:
            raise CheckpointError("FINGERPRINT_MISMATCH", f"source fingerprint mismatch: {path}")


def _read_sha256sums(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or not name:
            raise CheckpointError("BUNDLE_CHECKSUMS_MALFORMED", "SHA256SUMS is malformed")
        records[name] = digest
    return records


def _verify_bundle(repo: Path, manifest: Mapping[str, object], bundle_dir: Path) -> dict[str, str]:
    directory = bundle_dir.expanduser().resolve()
    manifest_path = directory / "checkpoint_manifest.json"
    bundle_path = directory / "checkpoint.bundle"
    sums_path = directory / "SHA256SUMS"
    for path in (manifest_path, bundle_path, sums_path):
        if not path.is_file():
            raise CheckpointError("BUNDLE_FILE_MISSING", f"local recovery file is missing: {path.name}")
    sums = _read_sha256sums(sums_path)
    for path in (manifest_path, bundle_path):
        if sums.get(path.name) != _sha256(path.read_bytes()):
            raise CheckpointError("BUNDLE_CHECKSUM_MISMATCH", f"checksum mismatch: {path.name}")
    expected_manifest = canonical_json_bytes(manifest) + b"\n"
    if manifest_path.read_bytes() != expected_manifest:
        raise CheckpointError("BUNDLE_MANIFEST_MISMATCH", "bundle manifest differs from tag manifest")
    _run_git(repo, "bundle", "verify", str(bundle_path))
    heads = _run_git(repo, "bundle", "list-heads", str(bundle_path)).stdout
    source_sha = str(manifest["source_commit_sha"])
    branch_ref = f"refs/heads/{manifest['source_branch']}"
    if f"{source_sha} {branch_ref}" not in heads.splitlines():
        raise CheckpointError("BUNDLE_SOURCE_REF_MISSING", "bundle does not contain the source branch ref")
    return {
        "bundle_path": str(bundle_path),
        "bundle_sha256": _sha256(bundle_path.read_bytes()),
    }


def verify_checkpoint(
    repo: Path,
    checkpoint: str,
    *,
    bundle_dir: Path | None = None,
) -> dict[str, object]:
    """Read-only verification of one local checkpoint tag and optional bundle."""
    root = _repo_root(repo)
    tag = checkpoint if checkpoint.startswith(TAG_PREFIX) else checkpoint_tag(checkpoint)
    headers, manifest = _manifest_from_tag(root, tag)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise CheckpointError("MANIFEST_SCHEMA_UNSUPPORTED", "unsupported checkpoint manifest schema")
    _validate_manifest_structure(manifest)
    manifest_hash = verify_manifest_hash(manifest)
    required_headers = {
        "checkpoint_id": str(manifest.get("checkpoint_id")),
        "source_commit_sha": str(manifest.get("source_commit_sha")),
        "manifest_sha256": manifest_hash,
        "layer_name": str(manifest.get("layer_name")),
        "final_status": "PASS",
        "layer_status": "CLOSED",
    }
    for key, expected in required_headers.items():
        if headers.get(key) != expected:
            raise CheckpointError("TAG_HEADER_MISMATCH", f"tag header mismatch: {key}")
    if manifest.get("checkpoint_tag") != tag:
        raise CheckpointError("TAG_NAME_MISMATCH", "manifest checkpoint_tag does not match the tag")
    if manifest.get("final_status") != "PASS" or manifest.get("layer_status") != "CLOSED":
        raise CheckpointError("CHECKPOINT_NOT_CLOSED", "checkpoint metadata is not PASS+CLOSED")
    source_sha = manifest.get("source_commit_sha")
    if not isinstance(source_sha, str):
        raise CheckpointError("SOURCE_COMMIT_MISSING", "manifest source commit is missing")
    source_exists = _run_git(root, "cat-file", "-e", f"{source_sha}^{{commit}}", check=False)
    if source_exists.returncode != 0:
        raise CheckpointError("SOURCE_COMMIT_MISSING", "manifest source commit does not exist")
    target = _run_git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}").stdout.strip()
    if target != source_sha:
        raise CheckpointError("TAG_TARGET_MISMATCH", "checkpoint tag does not target source_commit_sha")
    _verify_fingerprints(root, manifest, "config_fingerprints")
    _verify_fingerprints(root, manifest, "important_artifact_fingerprints")
    bundle_proof = _verify_bundle(root, manifest, bundle_dir) if bundle_dir else None
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "verification_status": "VERIFIED",
        "checkpoint_id": manifest["checkpoint_id"],
        "created_at_utc": manifest["created_at_utc"],
        "layer_name": manifest["layer_name"],
        "source_commit_sha": source_sha,
        "checkpoint_tag": tag,
        "tag_target_sha": target,
        "manifest_sha256": manifest_hash,
        "local_recovery_bundle": bundle_proof,
    }


def list_checkpoints(repo: Path) -> dict[str, object]:
    """List and independently verify every local ``checkpoint/*`` tag."""
    root = _repo_root(repo)
    tags = _run_git(root, "tag", "--list", f"{TAG_PREFIX}*", "--sort=refname").stdout.splitlines()
    entries: list[dict[str, object]] = []
    for tag in tags:
        try:
            proof = verify_checkpoint(root, tag)
            entries.append(
                {
                    "checkpoint_id": proof["checkpoint_id"],
                    "created_at_utc": proof["created_at_utc"],
                    "layer_name": proof["layer_name"],
                    "source_commit_sha": proof["source_commit_sha"],
                    "checkpoint_tag": tag,
                    "verification_status": "VERIFIED",
                }
            )
        except CheckpointError as exc:
            entries.append(
                {
                    "checkpoint_id": tag.removeprefix(TAG_PREFIX),
                    "created_at_utc": None,
                    "layer_name": None,
                    "source_commit_sha": None,
                    "checkpoint_tag": tag,
                    "verification_status": "FAILED",
                    "error_code": exc.code,
                }
            )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "checkpoint_count": len(entries),
        "checkpoints": entries,
    }


def failure_result(exc: CheckpointError) -> dict[str, object]:
    """Stable machine-readable failure envelope; never includes environment data."""
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "checkpoint_status": "NOT_CREATED",
        "verification_status": "FAILED",
        "error_code": exc.code,
        "error": str(exc),
    }


__all__ = [
    "DEFAULT_BUNDLE_ROOT",
    "MANIFEST_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "TAG_PREFIX",
    "CheckpointError",
    "CheckpointMetadata",
    "build_checkpoint_manifest",
    "checkpoint_tag",
    "create_checkpoint",
    "failure_result",
    "list_checkpoints",
    "normalize_checkpoint_id",
    "verify_checkpoint",
    "verify_manifest_hash",
]
