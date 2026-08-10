# Discovery Engine final holdout isolation v0

## Purpose

This slice prevents routine research code and Orchestrator agents from reading the
final holdout before the frozen hypothesis has passed validation. It adds an
authenticated encrypted artifact, an external key-provider contract, a frozen
evaluator identity, and a one-shot runner that returns bounded aggregate metrics.

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
   - a SHA-256 evaluator/spec hash.
3. The sealer writes an AES-256-GCM envelope. The envelope contains no plaintext
   file path and no key material.
4. `HoldoutSealStore` binds exactly one envelope hash, key id, evaluator id/hash,
   manifest hash, and hypothesis family.
5. Operationally, the plaintext source must then be removed from the normal
   research environment or moved to a separately protected staging location.
   Library code intentionally does not auto-delete source evidence.
6. Discovery/train/validation work proceeds without final-holdout plaintext.
7. Only after state `VALIDATION_PASSED` may the isolated runner begin preflight.
8. Preflight validates the envelope, registered seal, evaluator identity/hash and
   external key without decrypting final-holdout plaintext.
9. The runner irreversibly transitions the hypothesis family to
   `HOLDOUT_CONSUMED` **before** plaintext decryption.
10. The trusted evaluator runs in the runner process. Only up to 64 scalar numeric,
    boolean or null aggregate metrics may leave the runner through this API.
11. Success or post-consumption failure is appended to the tamper-evident
    Discovery result ledger. A failure after consumption is not retryable for the
    same hypothesis family.

## Cryptographic envelope

- cipher: AES-256-GCM;
- 32-byte external key;
- fresh 96-bit nonce per seal;
- authenticated header includes family id, frozen manifest hash, key id,
  evaluator id/hash, plaintext SHA-256 and plaintext size;
- a keyed HMAC over public header metadata permits key/evaluator deployment
  preflight before the one-shot claim;
- ciphertext authentication still occurs only during AES-GCM decryption, after
  the family entitlement has been consumed.

The unkeyed `envelope_hash` is an immutable artifact identity, not a substitute for
AES-GCM authentication.

## Key boundary

`HoldoutKeyProvider` is vendor-neutral. `EnvironmentKeyProvider` exists only as a
minimal local injection adapter. The secret must be injected into the trusted
sealer/runner process only. It must not be written to Git, manifests, SQLite task
metadata, audit artifacts, logs, prompts, or Orchestrator envelopes.

For stronger production isolation, replace the environment adapter with an OS or
external secret-manager adapter without changing the runner contract.

## Evaluator boundary

The evaluator is frozen before train/validation by `evaluator_id` plus a SHA-256
`evaluator_hash`. The runner rejects any evaluator that does not match the sealed
record before consuming the holdout.

The evaluator itself is trusted code. Python type contracts cannot sandbox a
malicious evaluator. Process/account/container isolation is therefore an
operational requirement when this moves beyond local research infrastructure.

## Deliberately excluded from v0

- no actual trading hypothesis;
- no parameter search or optimization;
- no broker/exchange writes;
- no order generation or execution;
- no real-money path;
- no automatic acceptance/rejection after final metrics;
- no automatic deletion of plaintext source data;
- no claim that local encryption defeats a host administrator.
