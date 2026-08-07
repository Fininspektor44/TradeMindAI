# TradeMind v1.32 Unified ECN Exporter

Purpose: replace three attached read-only MT5 exporters with one timer-driven EA on the research ECN account.

The unified EA embeds the existing proven collectors without changing their output contracts:

- ECN volume/microstructure: `TradeMindAI_Volume_v1_4/volume_*_M5.csv` and `ecn_manifest.csv`
- Risk snapshots: `TradeMindAI/mt5_risk_account_utc_<login>.csv`, `mt5_risk_positions_utc_<login>.csv`, `mt5_risk_symbols_utc_<login>.csv`
- Deal/history snapshots: `TradeMindAI/grid_account_<login>.csv`, `grid_positions_<login>.csv`, `grid_deals_<login>.csv`

Default cadence remains equivalent to the old three-EA layout:

- volume: 10 seconds, exporting only newly closed M5 bars after initial backfill
- risk: 30 seconds
- deals/history: 60 seconds

The wrapper owns one MT5 timer and dispatches each collector at its own cadence.

## Safety boundary

The exporter is read-only. It does not call `OrderSend`, `OrderSendAsync`, `PositionModify`, `PositionClose`, `CTrade.Buy` or `CTrade.Sell`. `OrderCalcMargin` remains in the risk collector because it only calculates informational margin values and does not place an order.

The installer copies and compiles the unified EA but deliberately does not remove or change existing attached exporters. The old three exporters should be removed from charts only after the unified EA is attached and fresh output files are verified.

## Install source on the ECN terminal

From the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_v132_unified_ecn_exporter.ps1" -Login "77053345"
```

After compile succeeds, refresh the MT5 Navigator, attach `TradeMindAI_ECN_UnifiedExporter_v1_32` to one chart, keep the forward-start date used for the clean ECN experiment in the deal-exporter input, and verify all three output families are fresh before removing the legacy exporter EAs.
