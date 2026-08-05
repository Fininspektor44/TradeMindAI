# TradeMind v1.16: Signal Intelligence Core

## Product goal

TradeMind is not a grid-signal copier and not a high-volume RSI alert bot.
The product scans market data continuously, rejects weak opportunities, and
surfaces only a small number of evidence-backed trade plans.

The user-facing result is a complete scenario:

- direction
- staged entries
- stop and invalidation
- targets
- the market reasons behind every level
- historical sample size
- raw and conservative reliability
- expected value after estimated costs

All complexity stays inside the platform. The customer receives a filtered,
explainable action plan rather than a pile of charts.

## Why the statistics exist

Statistics are a publication filter, not decoration. Every candidate is stored,
including rejected, losing, flat, and timed-out candidates. The database exists
to answer one question before publication:

> Did sufficiently similar setups produce a durable positive result after costs?

The system must never improve a public win rate by deleting losses, changing a
signal after movement starts, or silently changing what counts as a similar
setup.

## Three separate layers

### 1. Market observation

The observation layer gathers every useful feature available at the decision
time. The current schema supports:

- market structure and protected highs/lows
- BOS and CHoCH events
- buy-side and sell-side liquidity sweeps
- FVG direction and size
- Fibonacci retracement and OTE location
- volume, relative volume, tick activity, imbalance, and absorption
- momentum and displacement
- ATR, spread, volatility regime, and execution cost
- session and time context
- correlation and portfolio load
- macro, sentiment, and future custom features

Robot and grid monitoring may be recorded as separate context, but it has zero
weight in the default signal score and cannot be the primary trigger.

### 2. Research and evidence

Every market candidate receives a stable setup family and is evaluated after the
fact. The evidence record stores:

- wins, losses, flats, and total completed observations
- gross winning and losing R
- average win and average loss in R
- profit factor
- maximum drawdown in R
- recent and baseline win rate
- raw win rate
- Bayesian-smoothed win rate
- the lower 95% Wilson confidence bound

The raw number is not presented as certainty. For example, 35 wins from 43
observations may show about 81% raw reliability, while the conservative 95%
lower bound is only about 67%.

### 3. Publication gate

The publication gate never creates the setup. It decides whether an already
created market setup has enough support to leave shadow mode.

Default v1.16 requirements:

- market-data-generated candidate
- quality score at least 75/100
- at least 30 completed similar observations
- lower 95% Wilson bound at least 60%
- R-based profit factor at least 1.20
- expected value at least +0.05R after costs
- first target RR at least 1.20
- at least four contributing market-factor groups
- evidence not older than 24 hours
- no severe recent edge degradation

Failing sample, quality, or confidence keeps the candidate in `SHADOW_ONLY`.
Negative expected value, invalid source, stale evidence, weak RR, or severe edge
decay produces `REJECTED`. Only candidates passing every check become
`PUBLISHABLE`.

`PUBLISHABLE` still does not send anything. A later Telegram adapter must read
the decision and enforce its own delivery controls.

## Quality score

The quality score is a weighted description of the current market situation,
not a probability. Default weights are:

| Group | Weight |
|---|---:|
| Structure | 18% |
| Liquidity | 14% |
| Fibonacci / OTE | 10% |
| Volume | 12% |
| Momentum | 10% |
| Volatility | 10% |
| Confirmation | 10% |
| Session | 6% |
| Execution | 6% |
| Portfolio load | 4% |

The weights are policy configuration, not eternal truth. Research can replace
them when out-of-sample evidence proves that another weighting is stronger.
Unknown future feature groups can be added through versioned policy changes.

## Reliability and expected value

Raw reliability:

```text
wins / completed observations
```

Smoothed reliability:

```text
(wins + 1) / (completed + 2)
```

The publication gate uses the lower 95% Wilson bound, not the raw percentage.
Expected value is calculated in R:

```text
EV = p_low × average_win_R - (1 - p_low) × abs(average_loss_R) - costs_R
```

A high win rate with tiny winners and large losses is rejected when expected
value is negative.

## Immutable signal identity

The signal ID is generated from a canonical hash of everything known before the
move:

- observation and creation times
- symbol, timeframe, direction, and setup family
- all entries, stop, targets, and rationales
- market features
- factor scores and reasons
- provenance

Historical evidence is attached separately, so later database growth does not
change the original candidate ID.

Example:

```text
TM-20260805T133000Z-EURUSD-BUY-0f27c7f2b388e6a1
```

## Tamper-evident journal

Candidate, gate decision, publication, update, and outcome are separate append-
only events. Every event contains the previous event hash. Editing an old event
breaks the chain. A second candidate payload with the same signal ID but changed
content is rejected as an immutable-candidate mutation.

Losses are never deleted. A publication adapter may add an explanatory update,
but it cannot rewrite the original entry, stop, target, or timestamp.

## Current CLI

The v1.16 core accepts one candidate JSON and one evidence JSON:

```powershell
.\.venv\Scripts\python.exe -m trademind.signal_intelligence `
  --candidate .\data\signal_intelligence\candidate.json `
  --evidence .\data\signal_intelligence\evidence.json `
  --output .\data\signal_intelligence\passport.json `
  --journal .\data\signal_intelligence\events.jsonl `
  --cost-r 0.04
```

Output states:

- `PUBLISHABLE`
- `SHADOW_ONLY`
- `REJECTED`

## Safety contract

The v1.16 core:

- imports no broker API
- places no orders
- changes no robot settings
- sends no Telegram messages
- does not use grid signals as market triggers
- does not delete or rewrite outcomes
- evaluates candidates only from information available before publication

## Next implementation blocks

1. Convert current FX research observations into v1.16 candidate passports.
2. Build a versioned similarity key and evidence aggregator.
3. Run every candidate in shadow mode and record stop/target outcomes.
4. Add walk-forward and regime-separated validation.
5. Add a publication adapter only after the gate has a sufficient live sample.
