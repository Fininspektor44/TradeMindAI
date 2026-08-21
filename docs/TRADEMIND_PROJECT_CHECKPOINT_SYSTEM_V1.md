# TradeMind Project Checkpoint System V1

TradeMind checkpoints are explicit semantic close steps for safety-critical
engineering layers. An ordinary commit or passing test run never creates one.
A layer is operationally finalized only after all three states are true:

- `FINAL STATUS = PASS`
- `LAYER STATUS = CLOSED`
- `CHECKPOINT STATUS = CREATED + VERIFIED`

## Authoritative commands

Creation has one authority: `scripts/create_trademind_checkpoint.py`. The
PowerShell file is only an argument-preserving wrapper around that Python
command. Verification and listing are read-only operations provided by
`scripts/verify_trademind_checkpoint.py`.

macOS/Linux example:

```bash
.venv/bin/python scripts/create_trademind_checkpoint.py \
  --layer-name "SER8 PENDING ORDER LIFECYCLE + RISK CONTAINMENT V1" \
  --checkpoint-id ser8-pending-lifecycle-risk-containment-v1 \
  --final-status PASS \
  --layer-status CLOSED \
  --full-pytest-status PASS \
  --full-pytest-summary "2274 passed" \
  --ea-version 1.6 \
  --runtime-version python=3.12 \
  --demo-account 67206924 \
  --magic-number 990244 \
  --task-name TradeMindAI-SER8-Autonomous-Demo-Execution \
  --config config/risk_profiles/ser8_supervised_demo_v1.json \
  --create-bundle
```

Windows PowerShell example (the quoted layer name remains one argument):

```powershell
.\scripts\create_trademind_checkpoint.ps1 `
  --layer-name "SER8 PENDING ORDER LIFECYCLE + RISK CONTAINMENT V1" `
  --checkpoint-id ser8-pending-lifecycle-risk-containment-v1 `
  --final-status PASS `
  --layer-status CLOSED `
  --full-pytest-status PASS `
  --full-pytest-summary "2274 passed" `
  --create-bundle
```

Only explicitly supplied safe metadata is recorded. Repeated flags supply
multiple hypothesis IDs, holdout IDs, accepted research IDs, task names,
config files, artifacts, or notes. Critical files must be tracked inside the
repository; their bytes at the accepted source commit are SHA-256
fingerprinted. Values not supplied or provable remain null or empty. The
command never enumerates or serializes process environment variables.

## Identity and immutability

The normalized tag is `checkpoint/<checkpoint-id>`. Creation refuses dirty or
ambiguous repositories, detached HEAD, merge/rebase/cherry-pick state,
missing `origin`, existing local or remote tags, and non-PASS/non-CLOSED
metadata. It never uses force push, never deletes a tag, never stages files,
and pushes the current branch plus annotated tag atomically.

The annotated tag points directly to the accepted source commit and embeds
the canonical JSON manifest. Therefore V1 creates no metadata commit:

- `source_commit_sha` is the accepted code state and tag target.
- `checkpoint_commit_sha` is null.
- `manifest_sha256` hashes the canonical manifest without its own hash field.

The tag message includes the checkpoint ID, source SHA, manifest SHA, layer
name, PASS/CLOSED state, and full canonical manifest. This avoids circular
commit identity while keeping the metadata durable in Git and on the remote.
Duplicate names always fail closed, even if an existing tag appears similar.

## Remote completion and failure

Creation preflights the remote tag, creates the local annotated tag, optionally
builds and verifies the recovery bundle, then performs one non-force atomic
push of branch and tag. It reads the remote refs back and proves both the
branch and peeled annotated tag target equal `source_commit_sha`.

Any push or verification failure returns nonzero with machine-readable
`checkpoint_status: NOT_CREATED`. A local tag or recovery directory may remain
for diagnosis; they are never silently overwritten and success is never
reported for a partial remote operation.

## Local recovery bundle

`--create-bundle` writes only these files under the already-ignored location
`data/project_checkpoints/<checkpoint-id>/`:

- `checkpoint_manifest.json`
- `checkpoint.bundle`
- `SHA256SUMS`

The Git bundle contains the source branch and annotated checkpoint tag, so it
preserves Git history and refs without copying runtime datasets, broker
exports, credentials, or arbitrary working-tree files.

## Verify and list

```bash
.venv/bin/python scripts/verify_trademind_checkpoint.py \
  --checkpoint checkpoint/ser8-pending-lifecycle-risk-containment-v1

.venv/bin/python scripts/verify_trademind_checkpoint.py \
  --checkpoint checkpoint/ser8-pending-lifecycle-risk-containment-v1 \
  --bundle-dir data/project_checkpoints/ser8-pending-lifecycle-risk-containment-v1

.venv/bin/python scripts/verify_trademind_checkpoint.py --list
```

Verification is read-only. It checks annotated tag type and target, embedded
canonical manifest and SHA, source commit existence, PASS/CLOSED metadata,
source-commit file fingerprints, and optional bundle checksums/refs. Listing
returns checkpoint ID, creation time, layer, source SHA, tag, and independent
verification status for every local `checkpoint/*` tag.

## Safe recovery

Never hard-reset the current working branch as the default recovery method.

### A. Inspect without changing branches

```bash
git show checkpoint/ser8-pending-lifecycle-risk-containment-v1
git show --stat checkpoint/ser8-pending-lifecycle-risk-containment-v1^{}
```

### B. Create an isolated recovery branch

```bash
git switch -c recovery/ser8-pending-lifecycle-risk-containment-v1 \
  checkpoint/ser8-pending-lifecycle-risk-containment-v1^{}
```

### C. Restore from the remote tag

```bash
git fetch origin \
  refs/tags/checkpoint/ser8-pending-lifecycle-risk-containment-v1:refs/tags/checkpoint/ser8-pending-lifecycle-risk-containment-v1
git switch -c recovery/ser8-pending-lifecycle-risk-containment-v1 \
  checkpoint/ser8-pending-lifecycle-risk-containment-v1^{}
```

### D. Restore when the remote is unavailable

```bash
git clone data/project_checkpoints/ser8-pending-lifecycle-risk-containment-v1/checkpoint.bundle \
  TradeMindAI-recovery
cd TradeMindAI-recovery
git switch -c recovery/ser8-pending-lifecycle-risk-containment-v1 <source_commit_sha>
```

Run the verifier before using any recovered state operationally.
