# TradeMind v1.12 Live Signal Console

## Purpose

The first browser-based operational view of TradeMind. It presents research signals from the existing MT5 and Bybit pipelines in one read-only console. It does not place, modify or close orders.

## Existing foundation

TradeMind v1.11 already provides:

- MT5 ECN candle and observation journals;
- Bybit public-data collection and shadow signals;
- forward-only outcome evaluation;
- unified signal and state exports;
- watchdogs and stale-data detection;
- read-only safety guards.

v1.12 must consume these outputs instead of inventing another signal schema.

## User view

### Live signal feed

Every row contains:

- signal time;
- source: MT5 or BYBIT;
- symbol and timeframe;
- BUY or SELL;
- scenario and score;
- entry, stop and target;
- planned reward-to-risk ratio;
- current status;
- current MFE, MAE and result in R;
- data freshness.

### Signal details

The signal card shows:

- all reasons and confluence components;
- higher-timeframe bias;
- spread and market-cost context;
- structure, sweep and FVG observations;
- source record identifiers;
- lifecycle timestamps;
- final outcome and evaluation rules.

### Filters

- source;
- symbol;
- direction;
- scenario;
- score threshold;
- status;
- time range.

## Status model

- `NEW`: created but not yet evaluated;
- `ACTIVE`: future candles are being tracked;
- `WIN`: target reached first;
- `LOSS`: stop reached first;
- `TIMEOUT`: evaluation horizon expired;
- `CANCELLED`: invalidated by data or policy before activation;
- `STALE`: source data is no longer fresh.

`STALE` is a data-health overlay and must never be silently treated as an active signal.

## Technical shape

The MVP is a local Python web service running on Windows SER8.

- backend: Python standard library first, adding a framework only when justified;
- input: existing TradeMind CSV and JSON outputs;
- normalized view: existing unified signal fields;
- API: read-only JSON endpoints;
- frontend: lightweight HTML, CSS and JavaScript;
- refresh: polling every 5 seconds for the first release;
- deployment: one hidden Windows task;
- access: localhost by default.

## Initial endpoints

- `GET /api/health`
- `GET /api/signals`
- `GET /api/signals/{signal_key}`
- `GET /api/summary`
- `GET /`

No POST, PUT, PATCH or DELETE routes are allowed in v1.12.

## Safety contract

- `OrdersEnabled=False` remains mandatory;
- no broker or exchange credentials are read by the web service;
- no trading packages or order endpoints are added;
- all endpoints are read-only;
- stale inputs are visible in the UI;
- tests assert that no order submission symbols exist in the module.

## Delivery sequence

1. Contract tests for normalized signal loading.
2. Read-only signal repository over current CSV and JSON files.
3. JSON health, list and detail endpoints.
4. Live table with filters and 5-second refresh.
5. Signal detail card.
6. Windows launcher, checker and hidden scheduled task.
7. End-to-end comparison against source journals.

## Acceptance criteria

- the page opens locally on Windows SER8;
- MT5 and Bybit signals appear in one table;
- a new signal appears within one refresh cycle after the source journal changes;
- status and outcome update without restarting the service;
- stale data is clearly marked;
- values match source journals;
- `pytest` and `ruff check .` pass;
- no order-sending code exists.
