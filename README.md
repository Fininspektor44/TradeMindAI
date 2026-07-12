# TradeMind AI

TradeMind AI is an explainable market-screening and trader-analytics platform.

## Current milestone

`v0.1.0` establishes a clean, testable Python core before connecting the platform to MetaTrader 5 on the Windows mini-PC.

## Development model

- macOS: code, Git, tests, documentation
- Windows mini-PC: MetaTrader 5 gateway and 24/7 runtime
- No passwords, broker credentials, or Telegram tokens are stored in Git

## First modules

1. Configuration and logging
2. Market-data provider interface
3. Mock provider for safe local tests
4. MetaTrader 5 provider for Windows
5. Signal engine
6. Telegram notifications

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
trademind
pytest
```

The MT5 connector will be enabled only on the Windows runtime where MetaTrader 5 is installed and logged in.
