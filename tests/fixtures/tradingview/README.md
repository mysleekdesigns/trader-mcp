# TradingView cross-validation oracle fixtures

trader-mcp's event-driven interpreter is the source of truth. PRD §6 Phase 8 adds
**TradingView's Strategy Tester as a third independent cross-validation oracle** beside
`backtesting.py`. TradingView has **no public API** and its Chrome-DevTools-Protocol bridge
is fragile + ToS-gray, so this oracle is fed by a **manual CSV export** — a one-time,
ToS-clean, deterministic step you do by hand.

The test that consumes these fixtures is
[`tests/test_crossvalidate_tradingview.py`](../../test_crossvalidate_tradingview.py). It
**skips** unless both files below are present, so CI stays green without the proprietary
export. The pure parse/compare logic is unit-tested separately in
[`tests/test_tradingview_oracle.py`](../../test_tradingview_oracle.py) and runs always.

## What the test compares

**Trade decisions, not OHLCV.** TradingView's feed and intrabar fill model differ from the
engine's next-bar-open rule, so we do *not* require identical candles. For each trade we
check: same count (±1), same side, entry/exit within ±1 bar, and per-trade return within a
documented band (`max(0.5 pp, 5% relative)`). A cross landing one bar early/late is fine; a
different *decision* is a failure.

## Files

| File | Committed? | Role |
|---|---|---|
| `sma_cross_BTCUSD_1h.pine` | ✅ yes | Pine v5 strategy mirroring `_sma_cross_spec()`. Paste into TradingView. |
| `sma_cross_BTCUSD_1h.trades.example.csv` | ✅ yes | **Example only** — shows the expected columns. The test does **not** read it. |
| `sma_cross_BTCUSD_1h.trades.csv` | ❌ you create | Your real Strategy Tester *List of Trades* export. |
| `sma_cross_BTCUSD_1h.bars.json` | ❌ you create | The OHLCV the engine runs on (produced from trader-mcp's own cache). |

## Procedure (one time)

### 1. Produce the bars JSON — `sma_cross_BTCUSD_1h.bars.json`

Both engines must decide over the same candles. Use trader-mcp's own cache so the engine
side is hermetic and reproducible:

1. Sync the pinned window via the `sync_history` MCP tool (or the data layer directly):
   `exchange=coinbase`, `symbol=BTC/USD`, `timeframe=1h`, a **pinned UTC range** (pick one
   and record it here, e.g. `2024-01-01T00:00Z → 2024-03-31T00:00Z`).
2. Export those cached bars to JSON — a list of objects with
   `timestamp` (ISO 8601 UTC), `open`, `high`, `low`, `close`, `volume`:
   ```json
   [
     {"timestamp": "2024-01-01T00:00:00Z", "open": 42283.6, "high": 42554.6,
      "low": 42120.0, "close": 42475.2, "volume": 612.4}
   ]
   ```
   Save it here as `sma_cross_BTCUSD_1h.bars.json`.

### 2. Produce the TradingView export — `sma_cross_BTCUSD_1h.trades.csv`

1. Open **COINBASE:BTCUSD**, timeframe **1h**, in the TradingView app.
2. Pine Editor → paste `sma_cross_BTCUSD_1h.pine` → **Add to chart**.
3. Set the chart's visible range to the **same UTC window** you synced in step 1. Make
   timestamps UTC (Right-click the time axis → set timezone to **UTC**) — otherwise
   TradingView stamps exchange-local time and every bar index shifts.
4. Open **Strategy Tester ▸ List of Trades** and export to CSV (the ⤓ icon).
5. Save it here as `sma_cross_BTCUSD_1h.trades.csv`.

The parser matches columns fuzzily (handles `Type`/`Date/Time`/`Price`/`Profit %` naming
variants and either row order), so minor TradingView version differences are fine. A final
still-open trade (entry row with no exit) is dropped automatically.

### 3. Run it

```bash
uv run pytest -q tests/test_crossvalidate_tradingview.py
```

With both fixtures present the test runs the real comparison; without them it skips.

## Calibration

Start at the defaults (`bar_tol=1`, `return_tol_pp=0.5`, `return_rel_tol=0.05`,
`count_tol=1`) and tighten empirically — see the same discipline in
`test_crossvalidate_backtesting.py`. If trade-level alignment is clean but aggregate equity
drifts, that points at feed/commission differences, **not** interpreter logic — which is the
useful signal this oracle provides.
