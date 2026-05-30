---
name: run-backtest
description: >-
  Procedure to run a trader-mcp backtest correctly: ensure real cached data is
  synced, run the event-driven interpreter, cross-validate against
  backtesting.py, run a backtest<->live parity smoke check, and generate a
  quantstats tear sheet. Use when the user wants to backtest, optimize, or
  evaluate a strategy.
argument-hint: "[strategy] [symbol] [timeframe]"
---

# Run a backtest (trader-mcp)

Args: `$ARGUMENTS` (strategy / symbol / timeframe). Backtests use **real cached
data** and the **same interpreter as live** — never a separate backtest-only path.

## 1. Ensure data is cached
Confirm the (exchange, symbol, timeframe, range) is synced into the DuckDB/Parquet
store with no gaps. **Bybit first.** Sync via the historical-sync tool if missing.

## 2. Run the event-driven interpreter (source of truth)
Run the strategy spec through the custom interpreter on the cached bars. Keep it
deterministic (seeded, fixed range) so results are reproducible.

## 3. Cross-validate against backtesting.py
Run the same spec/data through the `backtesting.py` cross-check. Material drift
between the two is a **bug** — localize it before trusting results.

## 4. Parity smoke check (the invariant)
Replay the same cached history as a synthetic stream through the **live** path and
confirm signals match the backtest bar-for-bar. Any divergence ⇒ stop and fix
(loop in `backtest-engine-engineer` / `qa-parity-engineer`).

## 5. Optimize (optional)
For sweeps / walk-forward use vectorbt + Optuna. Guard against overfitting:
prefer walk-forward and out-of-sample windows; report robustness, not just the
best in-sample number.

## 6. Tear sheet
Generate a **quantstats** tear sheet and expose it as an MCP resource. Summarize
returns, drawdown, Sharpe/Sortino, and the fee/slippage assumptions used.

## Definition of done
Backtest on real cached data + cross-validation within tolerance + parity smoke
check passes + tear sheet produced, with assumptions stated.
