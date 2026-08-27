# AGENTS.md — universal instructions for any coding or AI agent

This applies to every AI agent (Claude, ChatGPT, Codex, or other) and to any
automated contributor working in this repository.

## 1. Read order — always, before doing anything

1. [`MASTER_CONTEXT.md`](MASTER_CONTEXT.md) — stable project truth: identity,
   goals, architecture, immutable decisions, safety rules, account roles, agent
   network, closed layers.
2. [`PROJECT_STATUS.md`](PROJECT_STATUS.md) — the current snapshot and the
   single `NEXT_ACTION`.
3. Only the `docs/` documents relevant to the current task (see the
   specialized-documents table in `MASTER_CONTEXT.md`).
4. [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md) — why past decisions were made,
   before proposing to change them.

## 2. Do not invent missing context

- If goals, architecture, rules, account roles, or the state of a layer are
  unclear or missing for your task, **stop and ask the operator**. Do not guess,
  do not assume, do not reconstruct project state from a commit message alone.
- Recalled facts and memory files reflect what was true when written — verify a
  named file, function, config key, account, or flag still exists before relying
  on it.
- If a document contradicts the repository, surface the contradiction; do not
  silently pick one.

## 3. Hard constraints

- Never weaken a safety rule in `MASTER_CONTEXT.md` (DEMO-only, manual approval,
  mandatory Risk Manager / authorization / one-shot claim, fail-closed,
  no secrets in Git).
- Never touch a closed / protected layer without an explicit recorded operator
  decision: historical acquisition + dataset identity, the deleted EMA/RSI
  `SignalEngine`, superseded screening / geometry result populations, Risk
  Manager lot-sizing + `standard_v1.json`, the accepted EURUSD hypothesis (a
  protected research artifact, not part of the CORE_8 execution universe), the
  protected final holdout, `checkpoint/*` tags, canonical account identities,
  and the consolidated scheduler (the disabled legacy scheduled tasks must not
  be re-enabled).
- Never connect a live account, never add an override / force / bypass flag to
  the execution path, never let MQL5 compute lot size.
- Do not run the full pytest suite, `git push`, or create a checkpoint unless
  the operator explicitly asks. Run focused tests for your change instead.
- Keep changes bounded and additive. Match the style and conventions of the
  surrounding code and docs.

## 4. Stage-closure documentation rule (Definition of Done)

A stage is **NOT complete** until all of the following are true:

1. **`PROJECT_STATUS.md` reflects the current state** — phase, branch /
   checkpoint, operational / completed / blockers / active work, a single clear
   `NEXT_ACTION`, and safety state. Remove stale content; do not append forever.
2. **`PROJECT_MEMORY.md` has an appended entry** for every material completed
   stage or decision: date/stage, what was done, what changed, the important
   decision and why, what remains, next step. No raw logs.
3. **`MASTER_CONTEXT.md` is updated only if stable truth changed** —
   architecture, immutable decisions, safety rules, account roles, agent
   network, or the set of closed layers. Keep it compact; it is not a history.

If your change also finalizes a safety-critical engineering layer
(`FINAL STATUS = PASS` and `LAYER STATUS = CLOSED`), also report
`CHECKPOINT REQUIRED: YES` and follow
[`docs/TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md`](docs/TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md)
— create the checkpoint only with explicit operator push authorization,
otherwise hand back the exact command.

This documentation refresh is the mechanism that makes project memory persist
across chats and models. It is not optional.

## 5. Reporting back

State plainly what changed, what was verified (and how), what was skipped, and
what the operator must do next. If tests failed or a step was skipped, say so.
