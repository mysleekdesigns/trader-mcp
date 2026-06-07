"""The one-interpreter parity invariant + determinism (the Phase 4 exit criteria).

These are the engine author's own guard rails; ``qa-parity-engineer`` layers the
full cross-cutting parity suite + backtesting.py cross-validation on top. Here we
assert the two execution modes of the SINGLE interpreter agree, and that a run is
reproducible.
"""

from __future__ import annotations

from trader_mcp.engine import BacktestConfig, SpecInterpreter, run_backtest
from trader_mcp.indicators import df_from_bars
from trader_mcp.strategy import (
    DCAConfig,
    EntryRules,
    ExitRules,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
)


def _spec() -> StrategySpec:
    return StrategySpec(
        name="ma-cross",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 5}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 20}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=50),
    )


def test_vectorized_matches_per_bar_signal_at(sine_bars: list) -> None:
    """signal_at(trailing window)[last] == signals(full)[i] for every bar i.

    THE one-interpreter invariant: feeding the same bars vectorized vs bar-by-bar
    yields identical signals. A trailing window long enough to warm the indicators
    reproduces the full-window value at its last row (indicators are causal).
    """
    spec = _spec()
    interp = SpecInterpreter(spec)
    df = interp.prepare(df_from_bars(sine_bars))
    vec = interp.signals(df)
    for i in range(len(df)):
        start = max(0, i - 30)  # warm-up window >= slowest indicator length
        window = df.iloc[start : i + 1]
        per_bar = interp.signal_at(window)
        assert (
            per_bar.enter_long,
            per_bar.enter_short,
            per_bar.exit_long,
            per_bar.exit_short,
        ) == (
            vec[i].enter_long,
            vec[i].enter_short,
            vec[i].exit_long,
            vec[i].exit_short,
        ), f"signal divergence at bar {i}"


def test_dca_cadence_parity_over_trailing_window() -> None:
    """DCA fires identically vectorized vs over the bounded trailing window.

    Regression guard for the parity bug QA pinned: the DCA cadence is anchored to
    an absolute timestamp grid ``(epoch_ms // tf_ms) % interval_bars == 0``, so it
    depends ONLY on the current bar -- ``signal_at(window)[-1]`` must equal
    ``signals(df)[i]`` for any window length, including the minimal 2-row window a
    live runtime keeps, and for a non-epoch-aligned start time.
    """
    from datetime import UTC, datetime, timedelta

    from trader_mcp.exchanges.models import OHLCVBar

    # A deliberately epoch-OFFSET start (07:00) -- the case the positional index
    # got wrong because window-local position != absolute bar index.
    t0 = datetime(2024, 1, 1, 7, 0, tzinfo=UTC)
    bars = [
        OHLCVBar(
            timestamp=t0 + timedelta(hours=i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
        )
        for i in range(30)
    ]
    for interval in (1, 2, 3, 5):
        spec = StrategySpec(
            name="dca",
            symbol="BTC/USD",
            timeframe="1h",
            strategy_type="dca",
            dca=DCAConfig(amount_quote=100.0, interval_bars=interval),
        )
        interp = SpecInterpreter(spec)
        df = interp.prepare(df_from_bars(bars))
        vec = interp.signals(df)
        for i in range(len(df)):
            window = df.iloc[max(0, i - 1) : i + 1]  # the bounded live buffer
            per_bar = interp.signal_at(window)
            assert per_bar.dca_buy == vec[i].dca_buy, (
                f"DCA cadence divergence at bar {i} (interval={interval})"
            )


def test_crossover_first_bar_never_signals() -> None:
    """A trailing window of exactly LIVE_WINDOW rows yields no spurious crossover."""
    spec = _spec()
    interp = SpecInterpreter(spec)
    from .conftest import make_bars

    bars = make_bars([100.0] * 25)
    df = interp.prepare(df_from_bars(bars))
    # The very first row can never be a crossover (no prior bar).
    first = interp.signal_at(df.iloc[0:1])
    assert not first.enter_long
    assert not first.enter_short


def test_backtest_is_deterministic(sine_bars: list) -> None:
    """Same (spec, bars, config) -> identical report_id, equity, and trades."""
    spec = _spec()
    cfg = BacktestConfig(initial_cash=10_000, slippage_pct=0.05, seed=7)
    a = run_backtest(spec, sine_bars, config=cfg)
    b = run_backtest(spec, sine_bars, config=cfg)
    assert a.report_id == b.report_id
    assert a.final_equity == b.final_equity
    assert len(a.trades) == len(b.trades)
    for ta, tb in zip(a.trades, b.trades, strict=True):
        assert ta.entry_price == tb.entry_price
        assert ta.exit_price == tb.exit_price
        assert ta.pnl == tb.pnl


def test_report_id_changes_with_config(sine_bars: list) -> None:
    """A different config (different inputs) yields a different content-addressed id."""
    spec = _spec()
    a = run_backtest(spec, sine_bars, config=BacktestConfig(initial_cash=10_000))
    b = run_backtest(spec, sine_bars, config=BacktestConfig(initial_cash=20_000))
    assert a.report_id != b.report_id
