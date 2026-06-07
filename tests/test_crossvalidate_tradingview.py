"""Cross-validate the trader-mcp engine against TradingView's Strategy Tester (PRD §6 Phase 8).

A THIRD independent cross-validation oracle beside ``backtesting.py`` (see
``test_crossvalidate_backtesting.py``). TradingView has no public API and its CDP bridge is
fragile + ToS-gray, so this oracle is fed by a **manual CSV export** -- it therefore SKIPS
unless both fixtures are present:

    tests/fixtures/tradingview/sma_cross_BTCUSD_1h.bars.json     (the OHLCV both engines see)
    tests/fixtures/tradingview/sma_cross_BTCUSD_1h.trades.csv    (TradingView List of Trades)

See ``tests/fixtures/tradingview/README.md`` for the exact, reproducible procedure (the Pine
mirror, the pinned COINBASE:BTCUSD 1h window, the Strategy Tester settings, and how to
produce the bars JSON via ``sync_history``). The Pine strategy
(``sma_cross_BTCUSD_1h.pine``) MUST stay in lock-step with ``_sma_cross_spec()`` below.

We compare **trade decisions, not OHLCV**: same trade count (+/-1), same side, entry/exit
within +/-1 bar, per-trade return within tolerance. This is robust to small TradingView<->CCXT
feed differences. The pure parse/compare logic is unit-tested in ``test_tradingview_oracle.py``;
this module is the end-to-end wiring against a real export.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests._tradingview_oracle import compare_trades, parse_tradingview_trades
from trader_mcp.engine import BacktestConfig, run_backtest
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
)

# Parameters mirrored EXACTLY by sma_cross_BTCUSD_1h.pine -- keep both in sync.
_FAST = 10
_SLOW = 30
_TAKER = 0.0006  # 0.06% taker == Pine commission_value=0.06
_SIZE_PCT = 95.0

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tradingview"
_BARS_PATH = _FIXTURE_DIR / "sma_cross_BTCUSD_1h.bars.json"
_TRADES_PATH = _FIXTURE_DIR / "sma_cross_BTCUSD_1h.trades.csv"


def _sma_cross_spec() -> StrategySpec:
    """The long-only fast/slow SMA-cross spec the Pine strategy mirrors."""
    return StrategySpec(
        name="sma-cross-tradingview-xval",
        exchange="coinbase",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": _FAST}),
            IndicatorSpec(id="slow", kind="sma", params={"length": _SLOW}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=_SIZE_PCT),
        risk=RiskLimits(stop_loss_pct=None, take_profit_pct=None),
        fees=Fees(taker=_TAKER, maker=_TAKER),
    )


def _load_bars(path: Path) -> list[OHLCVBar]:
    """Load the committed OHLCV fixture (list of bar dicts) into engine bars."""
    raw = json.loads(path.read_text())
    bars: list[OHLCVBar] = []
    for row in raw:
        ts = row["timestamp"]
        when = (
            datetime.fromisoformat(ts)
            if isinstance(ts, str)
            else datetime.fromtimestamp(ts / 1000, UTC)
        )
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        bars.append(
            OHLCVBar(
                timestamp=when,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
            )
        )
    bars.sort(key=lambda b: b.timestamp)
    return bars


def _fixtures_present() -> bool:
    return (
        _BARS_PATH.is_file()
        and _BARS_PATH.stat().st_size > 0
        and _TRADES_PATH.is_file()
        and _TRADES_PATH.stat().st_size > 0
    )


@pytest.mark.skipif(
    not _fixtures_present(),
    reason=(
        "TradingView oracle fixtures absent -- drop "
        "sma_cross_BTCUSD_1h.{bars.json,trades.csv} into tests/fixtures/tradingview/ "
        "(see that dir's README.md). Skipped, like the backtesting.py importorskip."
    ),
)
def test_crossvalidate_trades_against_tradingview() -> None:
    """trader-mcp's trade decisions match TradingView's Strategy Tester within tolerance.

    Decision-level comparison (count/side/entry-bar/exit-bar/return), NOT byte-identical
    equity -- TradingView's intrabar fill model and feed differ from the engine's
    next-bar-open rule, so we tolerate +/-1 bar and a documented return band. A mismatch
    here means a genuine divergence in *which* trades fire, which is what this oracle guards.
    """
    bars = _load_bars(_BARS_PATH)
    assert len(bars) > _SLOW, "fixture has too few bars to form a slow SMA"

    report = run_backtest(
        _sma_cross_spec(), bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0)
    )
    tv_trades = parse_tradingview_trades(_TRADES_PATH.read_text())

    assert report.metrics.trade_count > 0, "degenerate: engine produced no trades to compare"
    assert tv_trades, "degenerate: TradingView export contained no closed trades"

    bar_times = [b.timestamp for b in bars]
    mismatches = compare_trades(
        report.trades,
        tv_trades,
        bar_times,
        bar_tol=1,
        return_tol_pp=0.5,
        return_rel_tol=0.05,
        count_tol=1,
    )
    assert not mismatches, "trader-mcp <-> TradingView trade divergence:\n  " + "\n  ".join(
        str(m) for m in mismatches[:20]
    )
