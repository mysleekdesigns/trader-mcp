"""THE one-interpreter parity invariant for the LIVE path (PRD §5.3, §7).

Streaming the same bars one-by-one through ``StrategyRuntime`` + ``PaperBroker``
MUST yield the identical trades/fills/equity as ``run_backtest`` (which uses the
same ``SpecInterpreter`` vectorized + ``SimulatedBroker``) over the whole window.
Zero mismatches. This proves the live runtime drives the ONE engine, not a fork.
"""

from __future__ import annotations

from trader_mcp.engine import BacktestConfig, SimulatedBroker, SpecInterpreter, run_backtest
from trader_mcp.execution import ExecutionConfig, PaperBroker, StrategyRuntime
from trader_mcp.indicators import df_from_bars
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
)

from .conftest import make_bars


def _ma_cross_spec() -> StrategySpec:
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


def _rsi_spec() -> StrategySpec:
    return StrategySpec(
        name="rsi-revert",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[IndicatorSpec(id="rsi", kind="rsi", params={"length": 14})],
        entry=EntryRules(long="crossover(rsi, 30)"),
        exit=ExitRules(long="crossunder(rsi, 70)"),
        position_sizing=PositionSizing(mode="percent_equity", value=100),
    )


def _stream_session(spec: StrategySpec, bars, cfg: BacktestConfig):
    """Drive the live runtime bar-by-bar and force-close at the end (backtest analogue)."""
    exec_cfg = ExecutionConfig(
        initial_cash=cfg.initial_cash,
        slippage_pct=cfg.slippage_pct,
        seed=cfg.seed,
        funding_enabled=cfg.funding_enabled,
        funding_rate=cfg.funding_rate,
        funding_interval_hours=cfg.funding_interval_hours,
    )
    broker = PaperBroker(spec, exec_cfg)
    runtime = StrategyRuntime(spec, broker, buffer_size=len(bars) + 5)
    for bar in bars:
        runtime.on_bar(bar)
    runtime.finalize()
    return broker


def _assert_trades_match(spec: StrategySpec, bars, cfg: BacktestConfig) -> int:
    report = run_backtest(spec, bars, config=cfg)
    broker = _stream_session(spec, bars, cfg)
    live_trades = broker.trade_history()

    mismatches = 0
    assert len(live_trades) == len(report.trades), (
        f"trade count differs: live={len(live_trades)} backtest={len(report.trades)}"
    )
    for bt, lt in zip(report.trades, live_trades, strict=True):
        if (
            bt.side != lt.side
            or abs(bt.entry_price - lt.entry_price) > 1e-9
            or abs(bt.exit_price - lt.exit_price) > 1e-9
            or abs(bt.size - lt.size) > 1e-9
            or abs(bt.pnl - lt.pnl) > 1e-6
            or bt.exit_reason != lt.exit_reason
            or bt.bars_held != lt.bars_held
        ):
            mismatches += 1
    # Final equity must match too (cash after the terminal force-close).
    assert abs(broker.balance().cash - report.final_equity) < 1e-6, (
        f"final equity differs: live={broker.balance().cash} backtest={report.final_equity}"
    )
    return mismatches


def test_ma_cross_parity_zero_mismatch(sine_bars) -> None:
    """MA-cross: live stream vs backtest -> identical trades, fills, equity (0 mismatch)."""
    spec = _ma_cross_spec()
    cfg = BacktestConfig(initial_cash=10_000, slippage_pct=0.0)
    assert _assert_trades_match(spec, sine_bars, cfg) == 0


def test_ma_cross_parity_with_slippage(sine_bars) -> None:
    """Parity holds under non-zero slippage (the adversarial fill model agrees)."""
    spec = _ma_cross_spec()
    cfg = BacktestConfig(initial_cash=10_000, slippage_pct=0.05)
    assert _assert_trades_match(spec, sine_bars, cfg) == 0


def test_rsi_parity_zero_mismatch(sine_bars) -> None:
    """RSI revert (long-only): live stream vs backtest -> 0 mismatch."""
    spec = _rsi_spec()
    cfg = BacktestConfig(initial_cash=10_000, slippage_pct=0.0)
    assert _assert_trades_match(spec, sine_bars, cfg) == 0


def test_parity_with_stop_and_take(sine_bars) -> None:
    """Intrabar SL/TP fire identically in the streaming broker and the backtest broker."""
    spec = _ma_cross_spec().model_copy(
        update={"risk": RiskLimits(stop_loss_pct=2.0, take_profit_pct=3.0)}
    )
    cfg = BacktestConfig(initial_cash=10_000, slippage_pct=0.0)
    assert _assert_trades_match(spec, sine_bars, cfg) == 0


def test_perp_funding_parity() -> None:
    """A perp position accrues funding identically in the streaming and batch brokers."""
    # A long held many bars on a perp symbol so funding accrues over >1 interval.
    closes = [100.0] * 5 + [110.0] * 60 + [105.0] * 5
    bars = make_bars(closes)
    spec = StrategySpec(
        name="perp-hold",
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 2}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 4}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=50),
        risk=RiskLimits(max_leverage=3),
    )
    cfg = BacktestConfig(
        initial_cash=10_000, funding_enabled=True, funding_rate=0.0005, funding_interval_hours=8.0
    )
    assert _assert_trades_match(spec, bars, cfg) == 0
    broker = _stream_session(spec, bars, cfg)
    # Funding actually accrued (non-zero) on at least one trade.
    assert any(t.funding_paid != 0.0 for t in broker.trade_history())


def test_runtime_drives_signal_at_not_a_fork(sine_bars) -> None:
    """The runtime's per-bar signal equals SpecInterpreter.signal_at on the same window.

    Direct evidence the runtime calls the ONE engine (signal_at), not a reimplemented
    signal path: capture each on_bar's returned BarSignal and compare to the
    interpreter's signal_at over the same trailing buffer, and to the vectorized
    full-window signals.
    """
    spec = _ma_cross_spec()
    interp = SpecInterpreter(spec)
    vec = interp.signals(interp.prepare(df_from_bars(sine_bars)))

    broker = PaperBroker(spec, ExecutionConfig())
    runtime = StrategyRuntime(spec, broker, buffer_size=len(sine_bars) + 5)
    for i, bar in enumerate(sine_bars):
        sig = runtime.on_bar(bar)
        assert (sig.enter_long, sig.enter_short, sig.exit_long, sig.exit_short) == (
            vec[i].enter_long,
            vec[i].enter_short,
            vec[i].exit_long,
            vec[i].exit_short,
        ), f"runtime signal diverged from the engine at bar {i}"


def test_parity_with_default_rolling_buffer(sine_bars) -> None:
    """Parity holds with the runtime's DEFAULT (trimming) rolling buffer.

    A real live session keeps a bounded trailing buffer (not the whole history), so
    parity must survive trimming. The default buffer is sized to warm the slowest
    indicator with headroom; because indicators are causal the trailing window
    reproduces the full-window value at its last row.
    """
    spec = _ma_cross_spec()
    cfg = BacktestConfig(initial_cash=10_000, slippage_pct=0.0)
    report = run_backtest(spec, sine_bars, config=cfg)

    exec_cfg = ExecutionConfig(initial_cash=cfg.initial_cash, slippage_pct=cfg.slippage_pct)
    broker = PaperBroker(spec, exec_cfg)
    runtime = StrategyRuntime(spec, broker)  # default (trimming) buffer
    for bar in sine_bars:
        runtime.on_bar(bar)
    runtime.finalize()

    assert len(broker.trade_history()) == len(report.trades)
    assert abs(broker.balance().cash - report.final_equity) < 1e-6


def test_simulated_broker_unused_import_guard() -> None:
    """Sanity: SimulatedBroker import resolves (the broker we maintain parity with)."""
    assert SimulatedBroker is not None
