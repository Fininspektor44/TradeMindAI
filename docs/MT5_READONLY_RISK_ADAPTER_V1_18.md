# TradeMind MT5 Read-Only Risk Adapter v1.18

## Назначение

Этот блок связывает живой счёт MT5 с Risk Manager Core 1.0 без торгового API и без отправки ордеров.

Цепочка:

```text
MT5 account + positions + Market Watch specs
        ↓
TradeMind_MT5_Risk_Snapshot_Exporter.mq5
        ↓ CSV in Common\Files\TradeMindAI
trademind.mt5_risk_adapter
        ↓ validated JSON
AccountSnapshot + PortfolioSnapshot + InstrumentSpec
        ↓
Risk Manager Core 1.0
```

## Что экспортируется

### Account history

`mt5_risk_account_<login>.csv`

- balance;
- equity;
- used/free margin;
- leverage;
- open-position count;
- account trading permission;
- terminal connection state.

Account history is append-only. The adapter derives:

- high-watermark equity from all available rows;
- day-start equity from the first snapshot of the latest UTC day.

### Current positions

`mt5_risk_positions_<login>.csv`

The file is overwritten each refresh and contains every open position, including manual trades and positions from unrelated EAs. This is required so portfolio risk cannot ignore exposure outside TradeMind.

For each position the adapter calculates stop-defined money risk from:

```text
risk_money = distance(current_price, stop) / tick_size × tick_value_loss × volume
```

A position without a stop produces `risk_money=null`. Under the standard Risk Manager profile this blocks a new trade with `OPEN_RISK_UNKNOWN`.

### Market Watch instrument specifications

`mt5_risk_symbols_<login>.csv`

- tick size;
- tick value and loss tick value in account currency;
- volume min/max/step;
- contract size;
- trade mode;
- live buy/sell margin for one unit of volume calculated by MT5 `OrderCalcMargin`;
- leverage.

Only symbols visible in MT5 Market Watch are exported. A signal symbol absent from the latest snapshot fails closed.

## Freshness

Default maximum age is 120 seconds for account, positions and symbol specifications. Future timestamps beyond 30 seconds also fail closed.

The adapter retries a CSV read when the exporter is rewriting it. It never accepts a partially written header or an incomplete schema.

## Correlation groups

`config/mt5/correlation_groups_v1.json` is intentionally empty by default. Unlisted instruments use exact-symbol isolation:

```text
SYMBOL:EURUSD
```

Directional correlation groups can be added explicitly:

```json
{
  "symbols": {
    "EURUSD": {
      "BUY": "FX_USD_SHORT",
      "SELL": "FX_USD_LONG"
    }
  }
}
```

No statistical correlation is invented automatically.

## Installation

Copy the exporter to MT5 terminal data folders:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_v118_mt5_risk_exporter.ps1" -OpenFolder
```

Compile `TradeMind_MT5_Risk_Snapshot_Exporter.mq5` in MetaEditor and attach it to one chart on each account. Keep `InpOutputFolder=TradeMindAI`. No existing grid-exporter settings need to be changed.

## Adapter run

Example for account `67206924` and a BUY plan on EURUSD:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v118_mt5_risk_adapter.ps1" -Login 67206924 -Symbol EURUSD -Action BUY -RunTests
```

Output:

```text
data\mt5_risk_adapter_v1\67206924\account.json
data\mt5_risk_adapter_v1\67206924\portfolio.json
data\mt5_risk_adapter_v1\67206924\instrument_EURUSD.json
data\mt5_risk_adapter_v1\67206924\status.json
```

## Safety boundary

- no `OrderSend`;
- no position close or modify;
- no broker Python package;
- no terminal or robot setting changes;
- no signal publication;
- no execution approval.

`READY` means only that fresh, internally consistent MT5 snapshots were converted into Risk Manager inputs.
