# SER8 Multi-Symbol Historical Research Data V1

This layer is a read-only bridge from the real MT5 broker universe to
content-addressed market datasets and isolated strategy replay evidence. It
does not add execution symbols, create hypotheses, consume holdouts, publish
signals, or write to live candidate/outcome journals.

## Authoritative flow

1. `build_ser8_historical_data_inventory.py --mode verify-source` verifies the
   official MetaTrader5 Python capability, connected terminal, already
   authenticated market-data account `77053345`, broker/server identity, and
   the real execution-account universe `mt5_risk_symbols_utc_67206924.csv`.
2. The same command in `collect` mode processes every unique broker-exported
   symbol through deterministic calendar-month UTC chunks. Each successful
   chunk is identity-bound and staged for safe resume. It never imports an
   internet data source and never calls `symbol_select`, login, order, deal,
   position, or trade APIs.
3. Staged chunks are never canonical research inputs. Only after every chunk
   completes, identical boundary observations are deduplicated, conflicts fail
   closed, and merged bars pass integrity validation are canonical `bars.csv`
   plus `manifest.json` atomically published under
   `data/ser8_historical_market_data/<dataset_sha256>/`.
4. `replay_ser8_historical_data.py` reuses the production `SignalEngine`,
   `MarketStructureEngine`, FX candidate adapter, and conservative shadow
   outcome evaluator. It writes only below `data/ser8_historical_replay/`.
5. `discover_ser8_symbol_universe.py --historical-inventory ...` consumes the
   hash-verified replay readiness inventory. A legacy `symbol,rows` CSV proves
   availability only and cannot grant `RESEARCH_READY`.

The two account roles are intentionally separate. `67206924` is the DEMO
execution/research-target account whose export defines the universe;
`77053345` is the active ECN market-data account whose already-authenticated
terminal provides historical rates. Neither account is logged in, switched,
selected, or mutated by this layer. A market-data-only symbol cannot broaden
the execution universe.

The official MetaTrader5 Python API was selected because the repository had
no existing Python path that could acquire full-universe OHLC history. Existing
MT5 exporters cover other operational evidence, not this content-addressed
historical bar contract. The adapter uses only `initialize`, `terminal_info`,
`account_info`, `version`, `symbol_info`, and `copy_rates_range`. It requires
the operator's already authenticated terminal and deliberately accepts no
credential/login argument.

## Integrity and cross-account identity

The content identity binds both account logins, the canonical semantic
execution-universe hash, market-data broker/server/company identity, exact symbol identity,
available execution/source trade-tick-size compatibility, timeframe,
requested and actual coverage, the versioned calendar-month UTC chunk policy,
point/digits, expected interval, and exact canonical bar bytes.
`source_capture_utc`, chunk download order, cache/acquisition method, and retry
history are audit metadata excluded from dataset identity, so an identical
rerun is idempotent.

### Canonical execution-universe identity

Historical dataset schema `ser8-historical-market-data-v2` separates mutable
raw-export audit evidence from stable execution semantics:

- `execution_universe_raw_sha256` hashes the exact live CSV bytes for audit only.
  It is excluded from dataset identity.
- `execution_universe_sha256` is redefined as the canonical semantic SHA and
  equals `execution_universe_canonical_sha256`.
- `execution_universe_canonical_schema_version` is
  `ser8-execution-universe-canonical-v1`.
- The normalized snapshot is embedded in every dataset manifest and also
  atomically persisted under
  `data/ser8_historical_market_data/execution_universe_snapshots/<sha256>/snapshot.json`.
  Dataset verification recomputes its hash without reading the current live CSV.

The authoritative current schema emitted by both
`TradeMind_MT5_Risk_Snapshot_Exporter.mq5` and the demo executor is classified
explicitly: `time_msc`, `account_login`, `server`, `currency`, `symbol`,
`digits`, `trade_mode`, `bid`, `ask`, `tick_size`, `tick_value`,
`tick_value_profit`, `tick_value_loss`, `volume_min`, `volume_max`,
`volume_step`, `contract_size`, `margin_initial`, `margin_maintenance`,
`margin_buy_per_volume`, `margin_sell_per_volume`, `leverage`, and
`expiration_mode_flags`.

`time_msc`, `bid`, `ask`, `margin_buy_per_volume`, and
`margin_sell_per_volume` are AUDIT/VOLATILE. They remain protected by the
exact-byte raw SHA and are deliberately absent from the canonical semantic
rows. The two margin snapshots are not broker-policy constants: both MQL5
producers call `OrderCalcMargin` for volume `1.0`, passing the current
`tick.ask` for BUY and current `tick.bid` for SELL. The platform evaluates that
planned order on the current account in the current market environment and
returns the result in account currency; it does not include existing pending
orders or open positions. A quote, calculated margin snapshot, or capture-time
change therefore cannot change canonical identity. Every other declared
source field is IDENTITY-RELEVANT:

- execution-source and account compatibility: `account_login`, `server`,
  `currency`, `symbol`. Server text is trimmed but case-preserved; a genuine
  execution-account server change changes canonical identity and mixed-server
  rows fail closed;
- trade-direction eligibility: `trade_mode`;
- price precision and risk sizing: `digits`, `tick_size`, `tick_value`,
  `tick_value_profit`, `tick_value_loss`;
- order-volume constraints: `volume_min`, `volume_max`, `volume_step`;
- margin/notional fallback: `contract_size`, `margin_initial`,
  `margin_maintenance`, `leverage`;
- broker order-lifetime capability: `expiration_mode_flags`.

The snapshot also binds deterministic derived `asset_class` and
`risk_model_supported` fields. Numeric source values become finite canonical
decimal strings, symbols/enum text are normalized to uppercase, rows are
sorted by exact normalized symbol, identical normalized duplicates are
deduplicated, and conflicting duplicates fail closed. Any new unclassified
export column fails closed until this versioned classification is reviewed.

The ECN history is research evidence from account `77053345`; it is not a
claim that its spread or price feed is byte-identical to DEMO execution account
`67206924`. No cross-account normalization or equivalence is invented.

Each chunk cache binds source type, market-data account, server, symbol,
timeframe, exact UTC range, bar hash, row count, collector version, and code
hash. A missing, tampered, or identity-mismatched cache is reacquired. Final
merge sorts timestamps ascending, drops only value-identical duplicate
observations, and rejects conflicting values at the same timestamp. Validation
reports finite/numeric status, OHLC violations, gap counts, and largest gap. No
market observation is repaired, interpolated, forward-filled, or synthesized.
UTC weekend overlap is descriptive only; broker session rules are not guessed,
so gaps remain counted as unexplained until a broker-specific session calendar
is separately proven.

The compatibility CSV contains only datasets whose bar integrity passed. It is
not the research-readiness authority. Policy is explicit and versioned in
`config/research/ser8_historical_research_policy_v1.json`: initial timeframe
M5, 300-second interval, existing downstream minimum of 300 completed outcomes,
and the live shadow horizon/cost defaults.

MT5 rates provide OHLC, tick volume, bar spread, and real volume. They do not
provide the live exporter’s quote-direction microstructure. Replay records
that limitation in candidate provenance and gives those unavailable factors no
positive contribution; it never represents them as broker-observed facts.
Candidate decisions use only bars at or before candidate time. The shadow
evaluator receives only strictly later bars. An incomplete final horizon has no
fabricated outcome.

## Per-symbol available-coverage discovery

A broker is not obligated to retain history all the way back to the operator's
requested global start date. Rejecting an otherwise healthy symbol merely
because its broker-supported history is shorter than the requested window
would be a policy bug, not an integrity safeguard, so acquisition is governed
by an explicit, versioned discovery policy
(`ser8-backward-suffix-coverage-discovery-v1`) instead.

For each symbol the deterministic calendar-month chunk plan is attempted
backward from `requested_to_utc`. Every chunk that fetches or validates
successfully joins the accepted contiguous suffix. A chunk failure is never
itself the boundary — it is first positively classified:

- **Genuine historical unavailable** (a closed, explicit error-code allowlist)
  is the ONLY classification allowed to fix a truncated coverage boundary.
  Once fixed, every older chunk is recorded as `SKIPPED_UNAVAILABLE_PREFIX`
  without any further cache lookup or broker call — a genuinely absent broker
  prefix is never hammered chunk-by-chunk once its boundary is established,
  and a chunk cache already populated by a prior partial run still
  short-circuits the accepted suffix instantly.
- **Data-integrity failure** (a malformed/conflicting bar, or a merge conflict
  discovered between two already-accepted chunks) always fails the whole
  symbol closed: chunks already accepted are discarded rather than published
  as a "truncated" dataset that hides the failing chunk.
- **Transient/unresolved/unrecognized failure** (a plain read/acquisition
  error, or any error code not on the genuine-unavailable allowlist) gets a
  deterministic, bounded retry at the same chunk; if it still fails, the whole
  symbol is left unresolved and fails closed exactly like a data-integrity
  failure. It is never reinterpreted as "short broker history".

Because only a positively-classified genuine boundary can ever truncate
coverage, an intermittent valid/failed/valid pattern is never silently
bridged: a transient or integrity failure anywhere in the walk discards the
whole symbol — including chunks already accepted — rather than exposing a
partial suffix around it. `MetaTrader5HistorySource` positively classifies
"genuine unavailable" only when MT5's `last_error()` itself reports the
broker "not found" for an already-verified, visible symbol; every other
`copy_rates_range` failure (timeouts, connection errors, internal failures)
stays generic/transient and can never shorten coverage.

Every dataset manifest and inventory entry persists both the operator's
`requested_from_utc`/`requested_to_utc` (audit intent, unchanged), the
discovered `effective_coverage_start_utc`/`effective_coverage_end_utc`,
`coverage_truncated_at_requested_start`/`truncation_reason_code`, and the
explicit `unresolved_error_code`/`integrity_error_code`/
`merge_integrity_error_code` classification — alongside the accepted,
unavailable-prefix, discarded, and abandoned chunk audits — so an
older-history-unavailable symbol is visibly distinct from a disabled symbol,
an unreachable symbol, a transient acquisition failure, or a genuine
data-integrity failure, and is never silently relabeled as one of those.
Dataset identity binds the discovered effective coverage, not merely the
requested range, so two symbols that share a requested window but differ in
broker-supported history never collide. Acceptance and research readiness are
computed from this accepted actual coverage; a rejected/unavailable prefix
never lowers the accepted dataset's integrity bar, and the
minimum-rows-for-replay policy still applies only to what was actually
accepted.

### Historical-inventory JSON capacity

The full inventory aggregates every broker symbol's accepted, unavailable-
prefix, discarded, and abandoned chunk-audit records across the entire
requested calendar-month plan, plus the canonical execution-universe
snapshot and source/account provenance — legitimately far larger than any
single small provenance artifact (a candidate, a report projection, one
chunk-cache manifest). It is hashed and written under an explicit,
inventory-specific, still-finite `JsonSafetyBudget` (see
`HISTORICAL_INVENTORY_JSON_BUDGET`), deterministically sized for the
supported envelope with documented headroom above the real broker-export
symbol count and the documented multi-year monthly chunk plan. Every
unrelated JSON artifact keeps the original, stricter, module-wide default
budget completely unchanged; only the two call sites that hash/write the
full inventory pass the larger budget explicitly. A payload that still
exceeds the inventory-specific ceiling fails closed before any file is
touched, so a validation failure never leaves a partial canonical
`historical_inventory.json` behind.

## Windows verification — run one step, inspect it, then continue

Use the real repository and Common Files paths shown below. Keep the autonomous
task running; none of these commands addresses Task Scheduler.
Steps A1, A2, and B are already evidenced. C1 is the current next action. Run
C1-C4 sequentially and inspect each result before proceeding to D-F.

### A1. Read-only attachment identity check

This observation form intentionally omits `--terminal-path`. It never accepts
the attached terminal silently: source verification succeeds only if Python
attaches to active market-data login `77053345`; every other login fails closed.

```powershell
Set-Location "C:\Users\meff4\Documents\TradeMindAI"
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" `
  --mode verify-source `
  --execution-account 67206924 `
  --market-data-account 77053345 `
  --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI"
```

Stop and inspect `SOURCE_VERIFIED`, execution account `67206924`, market-data
account `77053345`, server, terminal identity, and the read-only operation list.
The active Python MT5 account must be `77053345`, not
`67206924`.

### A2. Production source verification with operator-proven terminal path

Replace the placeholder only after identifying the executable belonging to
active market-data account `77053345`. No executable path is assumed here.
This explicit-path verification is required before Steps B-F.

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" `
  --mode verify-source `
  --execution-account 67206924 `
  --market-data-account 77053345 `
  --terminal-path "<OPERATOR-PROVEN-77053345-TERMINAL64.EXE>" `
  --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI"
```

### B. Previously completed seven-day source proof

The original seven-day proof established that real broker bars are accessible.
It is retained below as historical procedure, not the next action.

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode collect --execution-account 67206924 --market-data-account 77053345 --terminal-path "<OPERATOR-PROVEN-77053345-TERMINAL64.EXE>" --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --timeframe M5 --from-utc "2026-08-14T00:00:00Z" --to-utc "2026-08-21T00:00:00Z" --proof-symbol-limit 1
```

Stop and inspect the one attempted entry, manifest quality, and the zero-order
safety fields. Do not treat the proof inventory as the full result.

### C. Chunked Windows acquisition sequence

### C1. Small multi-chunk acquisition proof

The real export is sorted deterministically; `--proof-symbol-limit 1` selects
one broker-derived symbol without adding an allowlist. The range crosses a UTC
calendar-month boundary and therefore proves more than one internal chunk.

```powershell
Set-Location "C:\Users\meff4\Documents\TradeMindAI"
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode collect --execution-account 67206924 --market-data-account 77053345 --terminal-path "C:\Program Files\RoboForex MT5 Terminal\terminal64.exe" --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --timeframe M5 --from-utc "2026-07-20T00:00:00Z" --to-utc "2026-08-21T00:00:00Z" --proof-symbol-limit 6
```

Stop and require multiple completed chunks, zero failed chunks, accepted real
bars, deterministic ascending output, and all zero broker-mutation fields.

### C2. Exact cache/idempotency rerun

Run the exact C1 command again. Require the attempted chunks to report cached,
the same dataset SHA, and no MT5 acquisition calls for valid cached chunks.

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode collect --execution-account 67206924 --market-data-account 77053345 --terminal-path "C:\Program Files\RoboForex MT5 Terminal\terminal64.exe" --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --timeframe M5 --from-utc "2026-07-20T00:00:00Z" --to-utc "2026-08-21T00:00:00Z" --proof-symbol-limit 6
```

### C3. Full-universe multi-year acquisition

This declared initial coverage window is explicit and may only be changed by
changing both UTC arguments deliberately.

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode collect --execution-account 67206924 --market-data-account 77053345 --terminal-path "C:\Program Files\RoboForex MT5 Terminal\terminal64.exe" --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --timeframe M5 --from-utc "2024-01-01T00:00:00Z" --to-utc "2026-08-21T00:00:00Z"
```

Stop and confirm `total_broker_symbols` matches the real export. Unavailable,
disabled, unsupported, insufficient, and integrity-failed symbols must remain
visible rather than disappearing.

### C4. Inventory summary

```powershell
$inventory = Get-Content ".\data\ser8_historical_market_data\historical_inventory.json" -Raw | ConvertFrom-Json
$inventory | Select-Object total_broker_symbols, accepted_dataset_count, chunk_policy_version, orders_sent, orders_canceled, positions_modified
$inventory.entries | Group-Object status | Sort-Object Name | Select-Object Name, Count
```

Stop and inspect the per-status counts plus each attempted entry's requested,
completed, empty, and failed chunk counts. C1-C4 are evidence gates; synthetic
pytest results cannot substitute for them.

### D. Integrity inventory verification

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode verify-inventory --execution-account 67206924 --market-data-account 77053345 --inventory "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_market_data\historical_inventory.json"
```

Stop and require `INVENTORY_VERIFIED` before replay.

### E. Deterministic isolated replay

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\replay_ser8_historical_data.py" --execution-account 67206924 --market-data-account 77053345 --historical-inventory "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_market_data\historical_inventory.json" --replay-root "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_replay" --output "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_replay\research_readiness.json"
```

Stop and inspect per-symbol candidate/outcome counts and readiness reasons. No
hypothesis is created and no final holdout is touched.

### F. Read-only discovery rerun

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\discover_ser8_symbol_universe.py" --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --execution-account 67206924 --market-data-account 77053345 --data-root "C:\Users\meff4\Documents\TradeMindAI\data" --historical-inventory "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_replay\research_readiness.json"
```

The real result may legitimately remain zero. Never substitute synthetic test
counts for the Windows inventory or replay result.
