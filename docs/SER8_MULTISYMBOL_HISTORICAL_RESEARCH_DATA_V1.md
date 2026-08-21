# SER8 Multi-Symbol Historical Research Data V1

This layer is a read-only bridge from the real MT5 broker universe to
content-addressed market datasets and isolated strategy replay evidence. It
does not add execution symbols, create hypotheses, consume holdouts, publish
signals, or write to live candidate/outcome journals.

## Authoritative flow

1. `build_ser8_historical_data_inventory.py --mode verify-source` verifies the
   official MetaTrader5 Python capability, connected terminal, already
   authenticated market-data account `37365712`, broker/server identity, and
   the real execution-account universe `mt5_risk_symbols_utc_67206924.csv`.
2. The same command in `collect` mode processes every unique broker-exported
   symbol. It never imports an internet data source and never calls
   `symbol_select`, login, order, deal, position, or trade APIs.
3. Canonical `bars.csv` plus `manifest.json` are atomically published under
   `data/ser8_historical_market_data/<dataset_sha256>/`.
4. `replay_ser8_historical_data.py` reuses the production `SignalEngine`,
   `MarketStructureEngine`, FX candidate adapter, and conservative shadow
   outcome evaluator. It writes only below `data/ser8_historical_replay/`.
5. `discover_ser8_symbol_universe.py --historical-inventory ...` consumes the
   hash-verified replay readiness inventory. A legacy `symbol,rows` CSV proves
   availability only and cannot grant `RESEARCH_READY`.

The two account roles are intentionally separate. `67206924` is the DEMO
execution/research-target account whose export defines the universe;
`37365712` is the ECN market-data account whose already-authenticated terminal
provides historical rates. Neither account is logged in, switched, selected,
or mutated by this layer. A market-data-only symbol cannot broaden the
execution universe.

The official MetaTrader5 Python API was selected because the repository had
no existing Python path that could acquire full-universe OHLC history. Existing
MT5 exporters cover other operational evidence, not this content-addressed
historical bar contract. The adapter uses only `initialize`, `terminal_info`,
`account_info`, `version`, `symbol_info`, and `copy_rates_range`. It requires
the operator's already authenticated terminal and deliberately accepts no
credential/login argument.

## Integrity and cross-account identity

The content identity binds both account logins, the execution-universe export
and hash, market-data broker/server/company identity, exact symbol identity,
available execution/source trade-tick-size compatibility, timeframe,
requested and actual coverage, point/digits, expected interval, and exact
canonical bar bytes. `source_capture_utc` is audit metadata and is excluded
from the dataset identity, so an identical rerun is idempotent.

The ECN history is research evidence from account `37365712`; it is not a
claim that its spread or price feed is byte-identical to DEMO execution account
`67206924`. No cross-account normalization or equivalence is invented.

Validation preserves source order and reports duplicate and out-of-order
timestamps, finite/numeric status, OHLC violations, gap counts, and largest
gap. No row is repaired, dropped, interpolated, forward-filled, or synthesized.
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

## Windows verification — run one step, inspect it, then continue

Use the real repository and Common Files paths shown below. Keep the autonomous
task running; none of these commands addresses Task Scheduler.
Only Step A is the current next action. Steps B-F are corrected here for later
controlled use; do not run them yet.

### A. Capability and source verification

```powershell
Set-Location "C:\Users\meff4\Documents\TradeMindAI"
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" `
  --mode verify-source `
  --execution-account 67206924 `
  --market-data-account 37365712 `
  --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI"
```

Stop and inspect `SOURCE_VERIFIED`, execution account `67206924`, market-data
account `37365712`, server, terminal identity, and the read-only operation list
before proceeding. The active Python MT5 account must be `37365712`, not
`67206924`.

### B. Small read-only acquisition proof

The limit selects the first symbol from the broker export; it is not a
handwritten symbol allowlist.

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode collect --execution-account 67206924 --market-data-account 37365712 --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --timeframe M5 --from-utc "2026-08-14T00:00:00Z" --to-utc "2026-08-21T00:00:00Z" --proof-symbol-limit 1
```

Stop and inspect the one attempted entry, manifest quality, and the zero-order
safety fields. Do not treat the proof inventory as the full result.

### C. Full-universe acquisition

This declared initial coverage window is explicit and may only be changed by
changing both UTC arguments deliberately.

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode collect --execution-account 67206924 --market-data-account 37365712 --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --timeframe M5 --from-utc "2024-01-01T00:00:00Z" --to-utc "2026-08-21T00:00:00Z"
```

Stop and confirm `total_broker_symbols` matches the real export. Unavailable,
disabled, unsupported, insufficient, and integrity-failed symbols must remain
visible rather than disappearing.

### D. Integrity inventory verification

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\build_ser8_historical_data_inventory.py" --mode verify-inventory --execution-account 67206924 --market-data-account 37365712 --inventory "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_market_data\historical_inventory.json"
```

Stop and require `INVENTORY_VERIFIED` before replay.

### E. Deterministic isolated replay

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\replay_ser8_historical_data.py" --execution-account 67206924 --market-data-account 37365712 --historical-inventory "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_market_data\historical_inventory.json" --replay-root "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_replay" --output "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_replay\research_readiness.json"
```

Stop and inspect per-symbol candidate/outcome counts and readiness reasons. No
hypothesis is created and no final holdout is touched.

### F. Read-only discovery rerun

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\discover_ser8_symbol_universe.py" --mt5-export-dir "C:\Users\meff4\AppData\Roaming\MetaQuotes\Terminal\Common\Files\TradeMindAI" --execution-account 67206924 --market-data-account 37365712 --data-root "C:\Users\meff4\Documents\TradeMindAI\data" --historical-inventory "C:\Users\meff4\Documents\TradeMindAI\data\ser8_historical_replay\research_readiness.json"
```

The real result may legitimately remain zero. Never substitute synthetic test
counts for the Windows inventory or replay result.
