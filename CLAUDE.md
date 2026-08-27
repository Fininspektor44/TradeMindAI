# CLAUDE.md

This repository keeps its own persistent memory. Do not rely on prior chat
history.

## Before doing anything

1. Read [`AGENTS.md`](AGENTS.md) — the universal agent operating rules.
2. Read [`MASTER_CONTEXT.md`](MASTER_CONTEXT.md) — stable project truth.
3. Read [`PROJECT_STATUS.md`](PROJECT_STATUS.md) — current snapshot and the
   single `NEXT_ACTION`.
4. Consult [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md) before changing any past
   decision, and open only the `docs/` files relevant to the task.

## Non-negotiables (full detail in `AGENTS.md` / `MASTER_CONTEXT.md`)

- Do not invent missing project state — if context is insufficient, ask the
  operator.
- Do not weaken a safety rule or touch a closed / protected layer.
- DEMO only. No live account, no execution-path override / force / bypass.
- Do not run the full pytest suite, `git push`, or create a checkpoint unless
  explicitly asked.
- A stage is not complete until `PROJECT_STATUS.md`, `PROJECT_MEMORY.md`, and
  (if stable truth changed) `MASTER_CONTEXT.md` are updated — see the
  stage-closure rule in `AGENTS.md`.
