# TradeMind v1.35 AdaptiveTrend reproduction

Source strategy: Duc Bui & Thanh Nguyen, **Systematic Trend-Following with Adaptive Portfolio Construction: Enhancing Risk-Adjusted Alpha in Cryptocurrency Markets**, arXiv:2602.11708 (2026).

The paper reports an out-of-sample test on 150+ crypto perpetuals for 2022-2024 with approximately 40.5% annualized return, Sharpe 2.41, maximum drawdown -12.7%, and Calmar 3.18. Its core rules are H6 momentum entries, ATR-based monotone trailing stops, monthly parameter optimization, Sharpe-based asset selection, and 70/30 long-short capital allocation.

## What v1.35 reproduces

- Bybit linear-perpetual H6 history from the public `/v5/market/kline` endpoint.
- Momentum `close(t)/close(t-L)-1`.
- Monthly grid search using only the immediately preceding calendar month.
- ATR trailing-stop multipliers 2.0, 2.5, 3.0, 3.5, matching the robust region reported by the paper.
- Long Sharpe gate 1.3 and short Sharpe gate 1.7.
- 70% long / 30% short allocation with equal weight inside each selected leg.
- Explicit costs per side.
- Read-only backtest outputs and monthly selection audit trail.

## Deliberate differences from the paper

The current TradeMind data stack does not store point-in-time historical market capitalization, so the paper's market-cap filtering stage is omitted instead of using present-day market cap and introducing look-ahead bias. The first v1.35 reproduction also does not reconstruct historical funding-rate charges. The paper states that momentum lookback and entry threshold are optimized monthly but the accessible text does not provide the exact grid, so v1.35 uses an explicit transparent grid in source code rather than pretending those values are known.

Therefore v1.35 is a **fixed-universe reproduction of the paper core**, not a claim that TradeMind will reproduce the paper's published statistics.

Safety: no orders, no publication, no private Bybit API, public market-data requests only.
