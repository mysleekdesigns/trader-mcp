"""Grid and DCA mechanics + perp funding."""

from __future__ import annotations

from trader_mcp.engine import BacktestConfig, run_backtest
from trader_mcp.strategy import (
    DCAConfig,
    EntryRules,
    ExitRules,
    Fees,
    GridConfig,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
)

from .conftest import make_bars


def test_grid_buys_low_sells_higher_level() -> None:
    """A grid buys when price dips to a level and realizes at the next level up."""
    spec = StrategySpec(
        name="grid",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="grid",
        grid=GridConfig(lower=90.0, upper=110.0, levels=5, allocation_pct=100.0),
        fees=Fees(taker=0.0, maker=0.0),
    )
    # Dip to 90 (touch lowest level) then rally through the next level (95) and up.
    closes = [100, 95, 90, 92, 96, 100, 105]
    bars = make_bars(closes, open_from_prev=True, low_mult=0.999)
    # Force the dip bar's low to exactly reach 90.
    bars[2] = bars[2].model_copy(update={"low": 90.0})
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    assert rep.trades, "grid should produce at least one realized slice"
    for t in rep.trades:
        assert t.side == "long"
        # Profitable slices exit above their entry (sold at a higher grid level).
        if t.exit_reason == "take_profit":
            assert t.exit_price > t.entry_price


def test_dca_accumulates_on_schedule() -> None:
    """DCA buys every interval_bars and realizes the stack at the end."""
    spec = StrategySpec(
        name="dca",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="dca",
        dca=DCAConfig(amount_quote=100.0, interval_bars=2, max_purchases=3),
        fees=Fees(taker=0.0, maker=0.0),
    )
    closes = [10] * 12
    bars = make_bars(closes, open_from_prev=True)
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    # One aggregate round-trip trade representing the accumulated base.
    assert len(rep.trades) == 1
    t = rep.trades[0]
    # 3 purchases * $100 / $10 = 30 base accumulated (flat price, no fees).
    assert abs(t.size - 30.0) < 1e-6


def test_dca_respects_max_purchases() -> None:
    """max_purchases caps total spend regardless of how many intervals elapse."""
    spec = StrategySpec(
        name="dca",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="dca",
        dca=DCAConfig(amount_quote=50.0, interval_bars=1, max_purchases=2),
        fees=Fees(taker=0.0, maker=0.0),
    )
    closes = [10] * 20
    bars = make_bars(closes, open_from_prev=True)
    rep = run_backtest(spec, bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    assert len(rep.trades) == 1
    # 2 purchases * $50 / $10 = 10 base.
    assert abs(rep.trades[0].size - 10.0) < 1e-6


def test_perp_funding_reduces_long_pnl() -> None:
    """A long perp pays funding when the rate is positive; spot pays none."""
    perp = StrategySpec(
        name="perp",
        symbol="BTC/USD:USD",  # swap notation -> perp
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 2}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 3}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        position_sizing=PositionSizing(mode="fixed_base", value=1.0),
        risk=RiskLimits(max_leverage=3.0),
        fees=Fees(taker=0.0, maker=0.0),
    )
    # Flat warm-up so SMA(2)/SMA(3) are valid before the move, then hold long
    # across many bars so several funding intervals elapse.
    closes = [10, 10, 10, 10, 11, 13, 15, 16, 17, 18, 17, 15, 12, 10]
    bars = make_bars(closes, open_from_prev=True)
    cfg = BacktestConfig(
        initial_cash=100_000,
        slippage_pct=0,
        funding_enabled=True,
        funding_rate=0.01,
        funding_interval_hours=2.0,
    )
    rep = run_backtest(perp, bars, config=cfg)
    assert rep.trades
    assert rep.trades[0].funding_paid > 0, "long should pay positive funding"
    assert rep.note is not None
    assert "funding" in rep.note.lower()


def test_funding_disabled_charges_nothing() -> None:
    """funding_enabled=False (or spot) charges zero funding."""
    spec = StrategySpec(
        name="perp",
        symbol="BTC/USD:USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 2}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 3}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        position_sizing=PositionSizing(mode="fixed_base", value=1.0),
        fees=Fees(taker=0.0, maker=0.0),
    )
    closes = [10, 11, 13, 15, 14, 12, 10]
    bars = make_bars(closes, open_from_prev=True)
    rep = run_backtest(spec, bars, config=BacktestConfig(funding_enabled=False))
    assert all(t.funding_paid == 0.0 for t in rep.trades)
