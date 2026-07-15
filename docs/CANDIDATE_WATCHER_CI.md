# Candidate Watcher CI Reference

This branch validates the complete TradeMind v1.2 candidate-watcher implementation.

The CI run must verify:

- candidate-history snapshots are deduplicated;
- promotion, rejection and loss events are classified correctly;
- the current-state file is atomically replaced;
- the full existing test suite still passes;
- Ruff reports no violations.

This file is documentation only. Candidate history remains read-only and does not affect signal
weights or order execution.
