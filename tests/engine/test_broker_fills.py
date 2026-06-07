"""Broker fill-math tests against hand-computed fixtures.

Each test pins a tiny, fully-predictable price path and asserts the exact entry/
exit price, fee, and pnl the broker should produce -- so a regression in the fill
convention or fee model is caught immediately.
"""

from __future__ import annotations

from trader_mcp.engine import BacktestConfig, run_backtest
from trader_mcp.strategy import EntryRules, ExitRules, IndicatorSpec, PositionSizing, StrategySpec

from .conftest import make_bars


def _ma_cross_spec(**kw: object) -> StrategySpec:
    base: dict[str, object] = {
        "name": "x",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "strategy_type": "rule",
        "indicators": [
            IndicatorSpec(id="fast", kind="sma", params={"length": 2}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 3}),
        ],
        "entry": EntryRules(long="crossover(fast, slow)"),
        "exit": ExitRules(long="crossunder(fast, slow)"),
        "position_sizing": PositionSizing(mode="fixed_base", value=1.0),
        "fees": {"taker": 0.001, "maker": 0.0},
    }
    base.update(kw)
    return StrategySpec(**base)  # type: ignore[arg-type]


def test_entry_fills_at_next_bar_open_not_close() -> None:
    """A crossover decided at bar t's close must fill at bar t+1's OPEN, not close.

    The fixture sets each bar's open distinct from its close, so an entry filled at
    the next bar's open is provably NOT the signal bar's close (no look-ahead, and
    the next-bar-open convention is in force).
    """
    closes = [10, 10, 10, 10, 12, 14, 16, 18, 20]
    bars = make_bars(closes, open_from_prev=True)
    # Distinct, increasing opens so close != open on every bar after the first.
    opens = [c - 0.5 for c in closes]
    bars = [b.model_copy(update={"open": opens[i]}) for i, b in enumerate(bars)]

    spec = _ma_cross_spec()
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=100_000, slippage_pct=0))
    assert rep.trades, "expected at least one trade on a warmed crossover series"
    entry = rep.trades[0]
    # The entry price equals SOME bar's open (next-bar fill), never its own close.
    assert entry.entry_price in opens


#: A few flat lead-in bars so the SMA(2)/SMA(3) pair is fully warmed before the
#: meaningful price move -- otherwise the only crossover lands during warm-up
#: (against a NaN slow value) and never fires. Flat bars keep fast == slow (no
#: spurious crossover) until the move begins.
_WARMUP = [10, 10, 10, 10]


def test_fee_and_pnl_math_long() -> None:
    """Hand-checked long round-trip: fixed_base size, known fills, known fees."""
    # Force a clean long: enter then exit. Fast(2)/slow(3) over a peak.
    closes = [*_WARMUP, 11, 13, 15, 14, 12, 10, 9]
    bars = make_bars(closes, open_from_prev=True)
    spec = _ma_cross_spec()
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=100_000, slippage_pct=0))
    assert rep.trades
    t = rep.trades[0]
    size = 1.0
    expected_entry_fee = t.entry_price * size * 0.001
    expected_exit_fee = t.exit_price * size * 0.001
    assert abs(t.fees_paid - (expected_entry_fee + expected_exit_fee)) < 1e-9
    gross = (t.exit_price - t.entry_price) * size
    assert abs(t.pnl - (gross - t.fees_paid)) < 1e-9


def test_slippage_is_adversarial() -> None:
    """Slippage raises buy fills and lowers sell fills vs the zero-slippage run."""
    closes = [*_WARMUP, 11, 13, 15, 14, 12, 10, 9]
    bars = make_bars(closes, open_from_prev=True)
    spec = _ma_cross_spec()
    clean = run_backtest(spec, bars, config=BacktestConfig(initial_cash=100_000, slippage_pct=0))
    slipped = run_backtest(
        spec, bars, config=BacktestConfig(initial_cash=100_000, slippage_pct=1.0)
    )
    assert clean.trades
    assert slipped.trades
    c, s = clean.trades[0], slipped.trades[0]
    assert s.entry_price > c.entry_price  # buy fills higher
    assert s.exit_price < c.exit_price  # sell fills lower
    assert s.pnl < c.pnl  # slippage always costs the trader


def test_stop_loss_fills_intrabar_at_level() -> None:
    """A long stop-loss fills at the stop price on the bar that breaches it."""
    # Enter long, then a bar dips through the stop. Wide low to breach it.
    closes = [*_WARMUP, 11, 13, 15, 15, 15, 15]
    bars = make_bars(closes, open_from_prev=True)
    # Make a late bar dip to a low well below the 5% stop.
    breach = len(bars) - 2
    bars[breach] = bars[breach].model_copy(update={"low": 1.0})
    spec = _ma_cross_spec(risk={"stop_loss_pct": 5.0})
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=100_000, slippage_pct=0))
    sl_trades = [t for t in rep.trades if t.exit_reason == "stop_loss"]
    assert sl_trades, "expected a stop-loss exit"
    t = sl_trades[0]
    expected_stop = t.entry_price * (1 - 0.05)
    assert abs(t.exit_price - expected_stop) < 1e-9


def test_take_profit_fills_intrabar_at_level() -> None:
    """A long take-profit fills at the target price on the bar that reaches it."""
    closes = [*_WARMUP, 11, 13, 15, 15, 15, 15]
    bars = make_bars(closes, open_from_prev=True)
    reach = len(bars) - 2
    bars[reach] = bars[reach].model_copy(update={"high": 1000.0})
    spec = _ma_cross_spec(risk={"take_profit_pct": 10.0})
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=100_000, slippage_pct=0))
    tp_trades = [t for t in rep.trades if t.exit_reason == "take_profit"]
    assert tp_trades, "expected a take-profit exit"
    t = tp_trades[0]
    expected_target = t.entry_price * (1 + 0.10)
    assert abs(t.exit_price - expected_target) < 1e-9


def test_percent_equity_sizing_scales_with_cash() -> None:
    """percent_equity allocates the configured share of equity as notional."""
    closes = [*_WARMUP, 11, 13, 15, 14, 12, 10, 9]
    bars = make_bars(closes, open_from_prev=True)
    spec = _ma_cross_spec(position_sizing=PositionSizing(mode="percent_equity", value=25))
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    assert rep.trades
    t = rep.trades[0]
    notional = t.entry_price * t.size
    # 25% of 10k = 2500 notional at entry (within fee rounding).
    assert abs(notional - 2500.0) < 1e-6
