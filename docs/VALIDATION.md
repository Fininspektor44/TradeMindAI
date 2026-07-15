# TradeMind Validation Methodology

TradeMind v1.1 validates research patterns separately for each instrument and forward horizon.
Cross-symbol aggregates are retained only as informational portfolio views.

## Chronological split

Evaluated non-overlapping trades are sorted by signal time and divided into an early half and a late
half. A pattern is unstable when either half loses its positive average net ATR or its PF_ATR falls to
1.0 or below.

## Promotion gates

`INSUFFICIENT_SAMPLE` means fewer than 30 evaluated non-overlapping trades, or too few trades in one
chronological half.

`UNSTABLE` means the total, early half or late half is not positive after spread.

`RESEARCH_CANDIDATE` means the total and both halves are positive, but the group has fewer than 300
trades or the lower approximate 95% confidence bound is not above zero.

`VALIDATED` requires at least 300 trades, positive total and both halves, and a positive lower CI95
bound for mean net ATR.

`PORTFOLIO_ONLY` marks cross-symbol aggregates. These rows cannot be promoted.

## Risk diagnostics

The validator reports maximum cumulative drawdown in ATR units and maximum consecutive losing-trade
streak. The confidence interval uses the sample standard deviation and a normal 1.96 multiplier. It is
an approximate research diagnostic, not a guarantee of future performance.

No validation status changes the live BUY, SELL or WAIT score in v1.1.
