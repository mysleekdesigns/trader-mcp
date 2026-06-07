"""No-look-ahead guarantees: a signal can only act on data at/after its own bar."""

from __future__ import annotations

from trader_mcp.engine import SpecInterpreter, run_backtest
from trader_mcp.indicators import df_from_bars
from trader_mcp.strategy import EntryRules, ExitRules, IndicatorSpec, PositionSizing, StrategySpec

from .conftest import make_bars


def test_future_bars_do_not_change_past_signals() -> None:
    """Truncating the series must not change signals on the bars that remain.

    If a signal at bar i depended on any bar > i (look-ahead), appending future
    bars would alter it. The interpreter must be strictly causal.
    """
    spec = StrategySpec(
        name="x",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[IndicatorSpec(id="rsi", kind="rsi", params={"length": 5})],
        entry=EntryRules(long="rsi < 40"),
        exit=ExitRules(long="rsi > 60"),
        position_sizing=PositionSizing(mode="percent_equity", value=10),
    )
    interp = SpecInterpreter(spec)
    closes = [100, 102, 99, 97, 95, 98, 103, 107, 104, 101, 99, 96, 94, 97, 100]
    full = make_bars(closes, open_from_prev=True)
    short = full[:10]

    sig_full = interp.signals(interp.prepare(df_from_bars(full)))
    sig_short = interp.signals(interp.prepare(df_from_bars(short)))

    for i in range(len(short)):
        assert (sig_full[i].enter_long, sig_full[i].exit_long) == (
            sig_short[i].enter_long,
            sig_short[i].exit_long,
        ), f"future bars leaked into past signal at bar {i}"


def test_signal_on_final_bar_never_executes() -> None:
    """A signal on the LAST bar has no next bar to fill at -> it must not trade.

    We engineer a series whose ONLY entry crossover is on the final bar, then
    assert no signal-entered trade exists (only a possible end_of_data close, which
    there cannot be here since nothing opened).
    """
    spec = StrategySpec(
        name="x",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 2}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 3}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=10),
    )
    interp = SpecInterpreter(spec)
    # Flat then a jump only on the very last close -> crossover on the final bar.
    closes = [10, 10, 10, 10, 10, 50]
    bars = make_bars(closes, open_from_prev=True)
    sigs = interp.signals(interp.prepare(df_from_bars(bars)))
    assert sigs[-1].enter_long, "fixture should crossover on the final bar"
    rep = run_backtest(spec, bars)
    assert all(t.exit_reason != "signal" for t in rep.trades)
    # No position was ever opened, so there are no trades at all.
    assert rep.trades == []
