# TradeMindAI Orchestrator v1 Specification

## Status

Architecture baseline for the first autonomous-development layer of TradeMindAI.

This document defines **Orchestrator v1 only**. It does not authorize live trading, broker execution,
strategy optimization or access to real-money accounts.

## 1. Goal

TradeMindAI Orchestrator is the control plane that removes the user from routine development and
research operations.

Its purpose is to:

- accept a high-level objective;
- turn it into bounded tasks;
- assign each task to a role;
- run deterministic tools and tests;
- request AI work only when necessary;
- preserve an auditable history of every decision and artifact;
- escalate to the user only when a high-value or high-risk decision is required.

The orchestrator is **not** a trading strategy and is **not** allowed to generate or execute orders in
v1.

## 2. Design principles

1. **Human by exception, not by default.** Routine work is autonomous. The user is contacted only for
   predefined approval gates, critical failures or irreversible actions.
2. **Models are replaceable workers.** No workflow may depend on one vendor or model name.
3. **Deterministic code owns execution.** AI may propose, review and explain. Deterministic code runs
   tests, validates schemas, evaluates rules and later generates production signals.
4. **Separation of duties.** The same role must not both author and approve a material change.
5. **Evidence before promotion.** No artifact advances because an agent says it is good. Promotion is
   based on machine-readable tests, validation gates and recorded evidence.
6. **Budget-aware AI usage.** 24/7 services run locally. Expensive model calls are event-driven and
   rate/budget limited.
7. **Read-only first.** Orchestrator v1 cannot place trades, publish signals or change broker risk.
8. **Every action is traceable.** Task state, prompts, model/provider, code diff, test output, hashes and
   approvals are journaled.

## 3. Roles

### 3.1 ARCHITECT

Responsibilities:

- define module boundaries and contracts;
- review whether proposed work matches the master architecture;
- identify systemic risk, coupling and scope creep;
- approve technical design before implementation.

The Architect may not self-approve its own implementation work.

### 3.2 DEVELOPER

Responsibilities:

- implement an approved task specification;
- write or update tests;
- produce a bounded diff;
- return machine-readable implementation metadata.

The Developer may not promote its own work to accepted state.

### 3.3 AUDITOR

Responsibilities:

- adversarially review architecture, code and test claims;
- search for bypasses, hidden state, look-ahead, overfitting and unsafe behavior;
- add regression tests for discovered defects;
- approve or reject implementation evidence.

The Auditor should receive the task goal and produced artifacts, but must not rely only on the
Developer's narrative summary.

### 3.4 OPERATOR

In v1 the Operator is deterministic local software, not a chat model.

Responsibilities:

- run commands, tests and health checks on SER8;
- collect logs and artifacts;
- enforce task state transitions;
- enforce budgets, timeouts and retry policies;
- send escalation notifications.

Later versions may extend this role to research runtimes and market monitoring, but not in v1.

## 4. Human approval gates

The orchestrator MUST stop and request explicit user approval for:

- enabling real-money trading or changing execution mode;
- adding a new broker/exchange credential with write permission;
- increasing risk limits;
- changing the master research methodology or holdout rules;
- deleting or rewriting protected historical evidence;
- merging a change classified as `ARCHITECTURE_BREAKING`;
- spending beyond the configured daily/monthly AI budget;
- any action the policy engine marks `HUMAN_REQUIRED`.

Routine refactors, test fixes, documentation updates and bounded research infrastructure work do not
require manual approval when all automatic gates pass.

## 5. Task state machine

Every orchestrated task has exactly one current state:

```text
NEW
  -> TRIAGED
  -> SPECIFIED
  -> ARCH_REVIEWED
  -> IMPLEMENTING
  -> TESTING
  -> AUDITING
  -> READY
  -> COMPLETED
```

Failure states:

```text
BLOCKED
REJECTED
FAILED
HUMAN_REQUIRED
CANCELLED
```

Rules:

- transitions are explicit and journaled;
- no backward transition silently overwrites history;
- a revision creates a new task revision linked to its predecessor;
- `COMPLETED` is immutable;
- `HUMAN_REQUIRED` may resume only after recorded user approval.

## 6. Core modules

Target package:

```text
src/trademind/orchestrator/
  __init__.py
  models.py
  state_machine.py
  policy.py
  budget.py
  task_store.py
  artifact_store.py
  audit_log.py
  dispatcher.py
  role_router.py
  tool_runner.py
  notification.py
  service.py
```

### 6.1 models.py

Defines immutable task, role, artifact, approval and run-result schemas.

Minimum task fields:

- `task_id`
- `revision`
- `parent_task_id`
- `created_at`
- `goal`
- `scope`
- `risk_class`
- `state`
- `assigned_role`
- `allowed_tools`
- `budget_limit`
- `acceptance_criteria`
- `artifact_refs`

### 6.2 state_machine.py

Owns all task transitions. Other modules may request a transition but may not mutate task state
directly.

### 6.3 policy.py

Determines whether an action is:

- `AUTO_ALLOWED`
- `AUTO_ALLOWED_WITH_AUDIT`
- `HUMAN_REQUIRED`
- `FORBIDDEN`

Initial forbidden actions in v1:

- place/modify/cancel a broker order;
- enable write-capable broker API execution;
- expose secrets to model prompts;
- bypass failed tests/audit;
- read protected Discovery Engine final holdout outside its gate;
- delete audit history.

### 6.4 budget.py

Tracks model usage and enforces limits.

Required controls:

- per-task call limit;
- per-role call limit;
- daily token/cost ceiling;
- monthly token/cost ceiling;
- cooldown after repeated failures;
- cache key for materially identical requests;
- automatic local-only fallback when budget is exhausted.

The system must be able to run 24/7 with **zero model calls** while no qualifying event exists.

### 6.5 task_store.py

Persistent task/revision store. SQLite is the preferred v1 backend because it is local, transactional
and dependency-light.

### 6.6 artifact_store.py

Stores references and hashes for:

- specifications;
- code patches;
- test reports;
- audit reports;
- research manifests;
- logs.

Large artifacts remain files on disk; the database stores metadata and SHA256 hashes.

### 6.7 audit_log.py

Append-only event log for orchestration decisions.

Every event records:

- timestamp;
- task/revision;
- actor role;
- model/provider when applicable;
- action;
- input artifact hashes;
- output artifact hashes;
- state transition;
- policy result;
- error/exit code.

Tamper-evident chaining should be used for protected audit records.

### 6.8 dispatcher.py

Selects the next runnable task according to dependency, priority, policy, budget and retry state.

### 6.9 role_router.py

Routes a task to an abstract role capability, not directly to a vendor.

Example configuration:

```yaml
roles:
  architect:
    provider: openai
    model: configurable
  developer:
    provider: anthropic
    model: configurable
  auditor:
    provider: openai
    model: configurable
```

Model/provider names are configuration, never hard-coded workflow logic.

### 6.10 tool_runner.py

Runs allow-listed local commands on SER8.

Requirements:

- working-directory allow-list;
- executable allow-list;
- timeout;
- captured stdout/stderr;
- exit code;
- environment-variable secret filtering;
- no arbitrary shell supplied directly by a model;
- command templates validated before execution.

### 6.11 notification.py

Initial destinations may be console/file. Telegram/email can be added later.

Notification classes:

- `INFO_DIGEST`
- `TASK_FAILED`
- `HUMAN_REQUIRED`
- `BUDGET_ALERT`
- `SECURITY_ALERT`

Routine successful steps should be aggregated into digests instead of notifying the user for every
action.

### 6.12 service.py

Long-running SER8 process. It performs scheduling, dispatch and recovery after restart.

It MUST be restart-safe: persisted tasks resume from the last durable state rather than from chat
memory.

## 7. AI interaction contract

Every model request is generated from a structured task envelope:

```json
{
  "task_id": "...",
  "role": "AUDITOR",
  "goal": "...",
  "scope": ["..."],
  "forbidden_actions": ["..."],
  "acceptance_criteria": ["..."],
  "artifacts": ["sha256:..."],
  "required_output_schema": "..."
}
```

Model output must be parsed into a versioned schema. Free-form prose may accompany the result but
must not be the sole control signal for an automated transition.

## 8. Initial autonomy boundary

Orchestrator v1 MAY autonomously:

- create implementation tasks from an approved specification;
- request architecture/developer/auditor model calls within budget;
- run unit/integration tests;
- run linters and deterministic audits;
- reject failed work;
- request a revision;
- write task/audit metadata;
- prepare a Git diff/branch for review;
- produce a daily digest.

Orchestrator v1 MUST NOT autonomously:

- merge into protected production branches;
- trade;
- change account risk;
- create or expose live broker credentials;
- reinterpret a failed research holdout as a tunable result;
- alter its own approval policy without human approval.

## 9. Integration with Discovery Engine

Discovery Engine remains a separate research package.

The orchestrator may:

- create a Discovery Engine implementation task;
- run its synthetic tests;
- collect manifests/ledger verification results;
- ask the Auditor role to attack its controls;
- mark work ready/rejected.

The orchestrator may NOT receive plaintext final-holdout data. Holdout protection remains a lower
level invariant owned by Discovery Engine infrastructure.

## 10. Future market/broker adapters

Market integrations are plug-ins below a common interface. Target families include:

- MT5/RoboForex;
- Bybit;
- Russian market adapters such as T-Bank/other broker or exchange APIs when separately implemented.

The orchestrator must not contain market-specific trading logic. Adding a market should add an adapter,
not require redesigning the control plane.

## 11. v1 acceptance criteria

Orchestrator v1 is complete when all of the following are demonstrated on synthetic/dev tasks:

1. process restart preserves all task states;
2. a task flows through Architect -> Developer -> tests -> Auditor without manual message copying;
3. Developer cannot self-approve;
4. failed tests prevent promotion;
5. Auditor rejection creates a revision instead of overwriting the old result;
6. `HUMAN_REQUIRED` blocks execution until approval is recorded;
7. model budget exhaustion stops model calls but does not crash local monitoring;
8. identical requests can be cached/deduplicated;
9. secrets are not included in model envelopes or captured logs;
10. every transition has a durable audit record;
11. no v1 code path can place a broker order;
12. the system can run idle for 24 hours without making an AI request.

## 12. First implementation slice

Build only the control skeleton:

1. `models.py`
2. `state_machine.py`
3. `policy.py`
4. `budget.py`
5. `task_store.py` using SQLite
6. `audit_log.py`
7. `dispatcher.py`
8. synthetic tests for state, policy, persistence and budget behavior

Do not integrate OpenAI, Anthropic, brokers, Telegram or market data in this slice.

The first slice proves that the control plane is deterministic and restart-safe before external agents
are allowed to act through it.
