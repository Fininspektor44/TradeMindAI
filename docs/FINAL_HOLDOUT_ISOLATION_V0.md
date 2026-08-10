# Discovery Engine final holdout isolation v0

## Purpose

This slice prevents routine research code and Orchestrator agents from reading the
final holdout before the frozen hypothesis has passed validation. It adds an
authenticated encrypted artifact, an external key-provider contract, a frozen
evaluator identity, plaintext quarantine, registry-level lifecycle guards, and a
one-shot runner that returns bounded aggregate metrics.

It does **not** make the host machine root/admin-proof. A Windows administrator or
malicious code already running inside the trusted sealer/runner process can still
break the local security boundary. The objective is to prevent accidental leakage,
agent access, repeated holdout probing, and ordinary research-process access.

## Lifecycle

1. The hypothesis and research manifest are frozen in `HypothesisRegistry`.
2. While the hypothesis is still exactly `FROZEN`, the trusted sealer receives:
   - the plaintext final-holdout artifact;
   - an externally injected AES-256 key;
   - a key id;
   - the frozen evaluator id;
   - a SHA-256 hash of the evaluator source/spec artifact.
3. The sealer writes an AES-256-GCM envelope. The envelope contains no plaintext
   file path and no key material.
4. `HoldoutSealStore` binds exactly one envelope hash, key id, evaluator id/hash,
   manifest hash, and hypothesis family.
5. `seal_and_quarantine` moves the plaintext source outside the declared research
   root into a disjoint quarantine directory, verifies the moved file hash, and
   records a path-free SHA-256 isolation attestation while the hypothesis is still
   `FROZEN`.
6. `HypothesisRegistry` refuses `FROZEN -> TRAIN_TESTED`,
   `TRAIN_TESTED -> VALIDATION_PASSED`, and
   `VALIDATION_PASSED -> HOLDOUT_CONSUMED` unless the registered holdout contains
   that persisted isolation attestation. The registry guard is independent of the
   Orchestrator bridge and closes direct lifecycle bypasses.
7. Discovery/train/validation work proceeds without final-holdout plaintext in the
   declared research root.
8. Only after state `VALIDATION_PASSED` may the isolated runner begin preflight.
9. Preflight validates the envelope, registered seal, the actual evaluator source
   file hash, the external key, and the absence of any prior family claim without
   decrypting final-holdout plaintext.
10. The runner appends `FINAL_HOLDOUT_CLAIM` to the tamper-evident result ledger.
    This is an independent one-shot anchor in addition to SQLite state.
11. The runner then irreversibly transitions the hypothesis family to
    `HOLDOUT_CONSUMED` **before** plaintext decryption.
12. The trusted evaluator runs in the runner process. Only up to 64 scalar numeric,
    boolean or null aggregate metrics may leave the runner through this API.
13. Success or post-claim failure is appended to the tamper-evident Discovery
    result ledger. A failure after the ledger claim is not retryable for the same
    hypothesis family.

The ledger claim deliberately makes the system fail closed. If the claim is
written and the subsequent SQLite transition or evaluation crashes, the family is
still considered burned. This is safer than permitting a second peek at the same
final holdout.

## Cryptographic envelope

- cipher: AES-256-GCM;
- 32-byte external key;
- fresh 96-bit nonce per seal;
- authenticated header includes family id, frozen manifest hash, key id,
  evaluator id/hash, plaintext SHA-256 and plaintext size;
- a keyed HMAC over public header metadata permits key/evaluator deployment
  preflight before the one-shot claim;
- ciphertext authentication still occurs only during AES-GCM decryption, after
  the family entitlement has been claimed.

The unkeyed `envelope_hash` is an immutable artifact identity, not a substitute for
AES-GCM authentication.

## Plaintext isolation boundary

The production-safe entry point is `FinalHoldoutSealer.seal_and_quarantine`.
Low-level `seal_file` exists for controlled staging/tests but does not create an
isolation attestation, so the registry will not allow research to advance from
`FROZEN` after only a low-level seal.

The quarantine directory must be disjoint from the declared research root. The
quarantined filename is derived from the plaintext SHA-256 rather than the source
filename. No quarantine path is written to the envelope, registry record,
Orchestrator task, or result ledger.

This is a local process/data-boundary control, not proof that an administrator did
not copy the plaintext elsewhere before quarantine.

## Dual one-shot boundary

Two independent records block ordinary repeat evaluation:

- `HypothesisRegistry` moves the family to `HOLDOUT_CONSUMED`;
- `ResultLedger` contains a tamper-evident `FINAL_HOLDOUT_CLAIM` for the family.

The runner checks both. A casual/manual SQLite reset therefore does not create a
fresh holdout entitlement while the ledger remains intact. An administrator who
can rewrite both the database and ledger files remains outside this local threat
model.

A cross-process lock next to the result ledger also prevents two runner processes
from racing the preflight/claim sequence. A stale lock after a hard crash fails
closed and requires explicit operator recovery.

## Key boundary

`HoldoutKeyProvider` is vendor-neutral. `EnvironmentKeyProvider` exists only as a
minimal local injection adapter. The secret must be injected into the trusted
sealer/runner process only. It must not be written to Git, manifests, SQLite task
metadata, audit artifacts, logs, prompts, or Orchestrator envelopes.

For stronger production isolation, replace the environment adapter with an OS or
external secret-manager adapter without changing the runner contract.

## Evaluator boundary

The evaluator is frozen before train/validation by `evaluator_id` plus a SHA-256
`evaluator_hash`. The isolated runner does not trust a hash declared by the
evaluator object. It hashes the actual source file defining the evaluator class and
requires that file to be the configured frozen evaluator artifact.

This binds the evaluator's defining source file, but it does not recursively hash
all imported dependencies. A later production evaluator package/spec manifest
should bind the complete dependency set before final-holdout use.

The evaluator itself is trusted code. Python type contracts cannot sandbox a
malicious evaluator. Process/account/container isolation is therefore an
operational requirement when this moves beyond local research infrastructure.

## Orchestrator boundary

`READ_PROTECTED_FINAL_HOLDOUT` remains a forbidden Orchestrator action. The
Discovery-Orchestrator bridge independently requires a registered protected
holdout plus isolation attestation before it can create a task. The task receives
only the path-free isolation receipt hash, never plaintext, quarantine paths, the
sealed artifact path, or key material.

This slice adds no ToolRunner template, agent tool, broker adapter, or model pathway
that can invoke the protected runner or obtain its external key.

## Deliberately excluded from v0

- no actual trading hypothesis;
- no parameter search or optimization;
- no broker/exchange writes;
- no order generation or execution;
- no real-money path;
- no automatic acceptance/rejection after final metrics;
- no claim that local encryption defeats a host administrator.
