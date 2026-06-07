"""Broadened backtest<->live parity matrix for the LIVE path (INVARIANT 1, PRD §5.3, §7).

This is the HARDENED, BROADENED companion to ``test_runtime_parity.py``. It proves
the one-interpreter invariant *end to end* -- not just at the signal layer, but at
the fill/trade/equity layer -- across the full strategy-type surface and the
fill-realism variants Phase 5 introduces:

    strategies : MA-cross, RSI mean-reversion, Donchian breakout, grid, DCA
    variants   : frictionless, slippage, perp + funding (where applicable)

For every (strategy x variant) cell we stream the SAME bars one-by-one through
``StrategyRuntime`` + ``PaperBroker`` (the live path) and run ``run_backtest``
(``SpecInterpreter`` vectorized + ``SimulatedBroker``) over the whole window (the
backtest path), then assert the trade list, every trade's fields, AND the terminal
equity are byte-for-byte identical. ZERO mismatches is the pass bar -- a single
divergence is a parity bug, reported with the FIRST diverging trade so it localizes.

We also re-prove, on the broadened set, that the runtime drives the ONE engine: its
per-bar ``BarSignal`` equals ``SpecInterpreter.signal_at`` / ``signals`` for the
same window (no forked signal logic). All deterministic: synthetic price paths are
built from fixed math / a seeded numpy ``Generator`` -- never wall-clock, never net.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

import pytest

from trader_mcp.engine import BacktestConfig, SpecInterpreter, run_backtest
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.execution import ExecutionConfig, PaperBroker, StrategyRuntime
from trader_mcp.indicators import df_from_bars
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
    build_from_template,
)

from .conftest import make_bars

# --------------------------------------------------------------------------- #
# Deterministic price paths (no wall-clock, no RNG seed drift)
# --------------------------------------------------------------------------- #


def _sine(n: int, *, mean: float = 100.0, amp: float = 12.0, period: float = 11.0) -> list[float]:
    """A smooth sine path that produces repeated MA/RSI/Donchian crossings."""
    return [mean + amp * math.sin(i / period) for i in range(n)]


# --------------------------------------------------------------------------- #
# Spec builders (rule specs use spot BTC/USD; perp variant overrides symbol)
# --------------------------------------------------------------------------- #


def _ma_cross_spec(**overrides) -> StrategySpec:
    base = {
        "name": "ma-cross",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "strategy_type": "rule",
        "indicators": [
            IndicatorSpec(id="fast", kind="sma", params={"length": 5}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 20}),
        ],
        "entry": EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        "exit": ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        "position_sizing": PositionSizing(mode="percent_equity", value=50),
    }
    base.update(overrides)
    return StrategySpec(**base)


def _rsi_spec(**overrides) -> StrategySpec:
    base = {
        "name": "rsi-revert",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "strategy_type": "rule",
        "indicators": [IndicatorSpec(id="rsi", kind="rsi", params={"length": 14})],
        "entry": EntryRules(long="crossover(rsi, 30)"),
        "exit": ExitRules(long="crossunder(rsi, 70)"),
        "position_sizing": PositionSizing(mode="percent_equity", value=100),
    }
    base.update(overrides)
    return StrategySpec(**base)


def _donchian_spec(**overrides) -> StrategySpec:
    base = {
        "name": "donchian-break",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "strategy_type": "rule",
        "indicators": [IndicatorSpec(id="dc", kind="donchian", params={"length": 10})],
        "entry": EntryRules(long="close > dc_upper", short="close < dc_lower"),
        "exit": ExitRules(long="close < dc", short="close > dc"),
        "position_sizing": PositionSizing(mode="percent_equity", value=50),
    }
    base.update(overrides)
    return StrategySpec(**base)


def _grid_spec(**overrides) -> StrategySpec:
    return build_from_template(
        "grid",
        {
            "symbol": "BTC/USD",
            "timeframe": "1h",
            "grid": {"lower": 85.0, "upper": 115.0, "levels": 10, "allocation_pct": 50.0},
            **overrides,
        },
    )


def _dca_spec(**overrides) -> StrategySpec:
    return build_from_template(
        "dca",
        {
            "symbol": "BTC/USD",
            "timeframe": "1h",
            "dca": {"amount_quote": 100.0, "interval_bars": 5, "max_purchases": None},
            **overrides,
        },
    )


# --------------------------------------------------------------------------- #
# The parity engine: stream the live path, run the backtest path, diff
# --------------------------------------------------------------------------- #


def _exec_cfg(cfg: BacktestConfig) -> ExecutionConfig:
    """The ExecutionConfig that mirrors a BacktestConfig 1:1 (same fills)."""
    return ExecutionConfig(
        initial_cash=cfg.initial_cash,
        slippage_pct=cfg.slippage_pct,
        seed=cfg.seed,
        funding_enabled=cfg.funding_enabled,
        funding_rate=cfg.funding_rate,
        funding_interval_hours=cfg.funding_interval_hours,
    )


_UNSET = object()


def _stream(
    spec: StrategySpec,
    bars: Sequence[OHLCVBar],
    cfg: BacktestConfig,
    *,
    default_buffer: bool = False,
    buffer_size: object = _UNSET,
) -> PaperBroker:
    """Drive the LIVE runtime bar-by-bar then force-close (the backtest's analogue).

    ``buffer_size`` selects the runtime's retention:
      * ``_UNSET`` (default) -> an explicit full-prefix cap (``len(bars)+5``);
      * ``default_buffer=True`` -> ``None`` (the runtime's DEFAULT unbounded
        full-prefix buffer -- exact parity for ALL indicators incl. EWM);
      * an explicit ``int`` -> a bounded trailing buffer (the documented
        approximation that may diverge for EWM indicators).
    """
    broker = PaperBroker(spec, _exec_cfg(cfg))
    if buffer_size is not _UNSET:
        chosen = buffer_size
    elif default_buffer:
        chosen = None
    else:
        chosen = len(bars) + 5
    runtime = StrategyRuntime(spec, broker, buffer_size=chosen)  # type: ignore[arg-type]
    for bar in bars:
        runtime.on_bar(bar)
    runtime.finalize()
    return broker


def _count_mismatches(
    spec: StrategySpec,
    bars: Sequence[OHLCVBar],
    cfg: BacktestConfig,
    *,
    default_buffer: bool = False,
    buffer_size: object = _UNSET,
) -> tuple[int, list[str]]:
    """Return (#mismatching trades, [human-readable first-divergence messages])."""
    report = run_backtest(spec, bars, config=cfg)
    broker = _stream(spec, bars, cfg, default_buffer=default_buffer, buffer_size=buffer_size)
    live = broker.trade_history()
    backtest = report.trades

    notes: list[str] = []
    if len(live) != len(backtest):
        notes.append(f"trade count: live={len(live)} backtest={len(backtest)}")
        return max(len(live), len(backtest)), notes

    mismatches = 0
    for i, (bt, lt) in enumerate(zip(backtest, live, strict=True)):
        diverged = (
            bt.side != lt.side
            or abs(bt.entry_price - lt.entry_price) > 1e-9
            or abs(bt.exit_price - lt.exit_price) > 1e-9
            or abs(bt.size - lt.size) > 1e-9
            or abs(bt.pnl - lt.pnl) > 1e-6
            or abs(bt.fees_paid - lt.fees_paid) > 1e-6
            or abs(bt.funding_paid - lt.funding_paid) > 1e-6
            or bt.bars_held != lt.bars_held
            or bt.exit_reason != lt.exit_reason
        )
        if diverged:
            mismatches += 1
            if len(notes) < 1:
                notes.append(
                    f"trade {i}: backtest(side={bt.side},entry={bt.entry_price:.6f},"
                    f"exit={bt.exit_price:.6f},pnl={bt.pnl:.6f}) vs "
                    f"live(side={lt.side},entry={lt.entry_price:.6f},"
                    f"exit={lt.exit_price:.6f},pnl={lt.pnl:.6f})"
                )

    # Terminal equity (cash after the force-close) must agree too.
    if abs(broker.balance().cash - report.final_equity) > 1e-6:
        mismatches += 1
        notes.append(
            f"final equity: live={broker.balance().cash:.6f} backtest={report.final_equity:.6f}"
        )
    return mismatches, notes


def _assert_zero(
    spec: StrategySpec,
    bars: Sequence[OHLCVBar],
    cfg: BacktestConfig,
    *,
    default_buffer: bool = False,
    buffer_size: object = _UNSET,
) -> None:
    mismatches, notes = _count_mismatches(
        spec, bars, cfg, default_buffer=default_buffer, buffer_size=buffer_size
    )
    assert mismatches == 0, f"{spec.strategy_type}/{spec.name}: {mismatches} mismatch(es): {notes}"


# --------------------------------------------------------------------------- #
# The matrix: strategy x variant -> 0 mismatch
# --------------------------------------------------------------------------- #

_FRICTIONLESS = BacktestConfig(initial_cash=10_000, slippage_pct=0.0)
_SLIPPAGE = BacktestConfig(initial_cash=10_000, slippage_pct=0.05)


# Rule strategies on spot, frictionless + slippage. Each builder + a sine path.
_RULE_CELLS = [
    ("ma_cross", _ma_cross_spec, _FRICTIONLESS),
    ("ma_cross+slip", _ma_cross_spec, _SLIPPAGE),
    ("rsi", _rsi_spec, _FRICTIONLESS),
    ("rsi+slip", _rsi_spec, _SLIPPAGE),
    ("donchian", _donchian_spec, _FRICTIONLESS),
    ("donchian+slip", _donchian_spec, _SLIPPAGE),
]


@pytest.mark.parametrize(("label", "builder", "cfg"), _RULE_CELLS, ids=[c[0] for c in _RULE_CELLS])
def test_rule_strategy_parity_zero_mismatch(label, builder, cfg) -> None:
    """Rule strategy x {frictionless, slippage}: live stream == backtest (0 mismatch)."""
    bars = make_bars(_sine(400))
    _assert_zero(builder(), bars, cfg)


# Grid + DCA on spot, frictionless + slippage. Price path stays inside the grid band.
_NONRULE_CELLS = [
    ("grid", _grid_spec, _FRICTIONLESS),
    ("grid+slip", _grid_spec, _SLIPPAGE),
    ("dca", _dca_spec, _FRICTIONLESS),
    ("dca+slip", _dca_spec, _SLIPPAGE),
]


@pytest.mark.parametrize(
    ("label", "builder", "cfg"), _NONRULE_CELLS, ids=[c[0] for c in _NONRULE_CELLS]
)
def test_nonrule_strategy_parity_zero_mismatch(label, builder, cfg) -> None:
    """Grid + DCA x {frictionless, slippage}: live stream == backtest (0 mismatch)."""
    bars = make_bars(_sine(300))
    _assert_zero(builder(), bars, cfg)


@pytest.mark.parametrize(
    "leverage",
    [1, 2, 5],
)
def test_perp_funding_parity_matrix(leverage: int) -> None:
    """A perp held across multiple funding intervals: live == backtest at every leverage.

    Funding accrual, leverage-scaled sizing, and the terminal force-close all agree
    to the cent between the streaming PaperBroker and the batch SimulatedBroker.

    NOTE: the long is entered WELL PAST the runtime's warm-up gate (the entry
    crossover lands ~bar 30, not bar 4) so this test isolates the funding/leverage
    parity and is NOT confounded by the separate warm-up-gate parity bug captured by
    ``test_warmup_gate_suppresses_valid_entry_signal_bug`` below.
    """
    # Flat warm-up, then a step up around bar 30 (a crossover well past the gate),
    # held long enough to accrue funding over many 8h intervals, then a step down.
    closes = [100.0] * 30 + [112.0] * 80 + [104.0] * 6
    bars = make_bars(closes)
    spec = _ma_cross_spec(
        name="perp-hold",
        symbol="BTC/USDT:USDT",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 2}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 4}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        risk=RiskLimits(max_leverage=leverage),
    )
    cfg = BacktestConfig(
        initial_cash=10_000, funding_enabled=True, funding_rate=0.0005, funding_interval_hours=8.0
    )
    _assert_zero(spec, bars, cfg)
    # Non-vacuous: funding actually accrued on at least one closed trade.
    broker = _stream(spec, bars, cfg)
    assert any(t.funding_paid != 0.0 for t in broker.trade_history()), (
        "expected non-zero funding so the parity assertion is meaningful"
    )


@pytest.mark.parametrize(
    "sizing_mode",
    ["percent_equity", "fixed_quote", "fixed_base"],
)
def test_sizing_mode_parity(
    sizing_mode: Literal["percent_equity", "fixed_quote", "fixed_base"],
) -> None:
    """Every sizing mode produces identical fills on the live and backtest paths."""
    value = {"percent_equity": 40.0, "fixed_quote": 2_500.0, "fixed_base": 12.0}[sizing_mode]
    spec = _ma_cross_spec(position_sizing=PositionSizing(mode=sizing_mode, value=value))
    bars = make_bars(_sine(400))
    _assert_zero(spec, bars, _FRICTIONLESS)


def test_stop_and_take_parity() -> None:
    """Intrabar SL/TP fire identically in the streaming broker and the batch broker."""
    spec = _ma_cross_spec(risk=RiskLimits(stop_loss_pct=2.0, take_profit_pct=3.0))
    # Widen the HL range so intrabar stop/take levels are actually breached.
    bars = make_bars(_sine(400), high_mult=1.02, low_mult=0.98)
    _assert_zero(spec, bars, _FRICTIONLESS)


# The runtime's DEFAULT buffer is now unbounded (full prefix), so parity holds for
# EVERY indicator family under the default. This row covers finite-window (SMA,
# Donchian) and the stateless grid/DCA cadences; EWM specs (RSI/EMA/MACD) get their
# own dedicated default-buffer parity tests below.
@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("ma_cross_sma", _ma_cross_spec),  # _ma_cross_spec uses SMA (finite window)
        ("donchian", _donchian_spec),
        ("grid", _grid_spec),
        ("dca", _dca_spec),
    ],
)
def test_parity_holds_with_default_buffer(label, builder) -> None:
    """Parity holds under the runtime's DEFAULT (unbounded) buffer for these specs.

    The default buffer retains the full prefix, so the trailing-window indicator
    value equals the full-window value at the last row for finite-window indicators
    (SMA/Donchian) and the stateless grid/DCA cadences -- identical trade list and
    terminal equity to the backtest.
    """
    n = 300 if label in ("grid", "dca") else 400
    bars = make_bars(_sine(n))
    _assert_zero(builder(), bars, _FRICTIONLESS, default_buffer=True)


# --------------------------------------------------------------------------- #
# Direct evidence: the runtime drives the ONE engine (signal_at), not a fork
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("ma_cross", _ma_cross_spec),
        ("rsi", _rsi_spec),
        ("donchian", _donchian_spec),
    ],
)
def test_runtime_per_bar_signal_equals_signal_at_on_same_window(label, builder) -> None:
    """The runtime drives the ONE engine: its per-bar BarSignal == signal_at(buffer).

    Direct, bug-independent evidence that ``StrategyRuntime.on_bar`` reimplements NO
    signal logic -- it asks ``SpecInterpreter.signal_at`` over its OWN trailing
    buffer. We re-derive the expected signal by calling ``signal_at`` on the exact
    same prepared trailing window the runtime holds at each bar (mirroring its
    warm-up gate) and assert byte-for-byte equality. This holds regardless of the
    warm-up-gate or default-buffer parity bugs (those are about whether that window
    matches the *vectorized full-window* value, asserted separately).
    """
    spec = builder()
    bars = make_bars(_sine(400))
    runtime_interp = SpecInterpreter(spec)

    broker = PaperBroker(spec, ExecutionConfig())
    runtime = StrategyRuntime(spec, broker, buffer_size=len(bars) + 5)

    mismatches: list[int] = []
    buffer: list[OHLCVBar] = []
    for i, bar in enumerate(bars):
        sig = runtime.on_bar(bar)
        buffer.append(bar)
        got = (sig.enter_long, sig.enter_short, sig.exit_long, sig.exit_short)
        if len(buffer) < runtime._min_signal_bars():
            want = (False, False, False, False)
        else:
            window = runtime_interp.prepare(df_from_bars(buffer))
            expected = runtime_interp.signal_at(window)
            want = (
                expected.enter_long,
                expected.enter_short,
                expected.exit_long,
                expected.exit_short,
            )
        if got != want:
            mismatches.append(i)
    assert not mismatches, (
        f"{label}: runtime signal diverged from signal_at on its OWN window at "
        f"{len(mismatches)} bar(s); first at bar {mismatches[0]}"
    )


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("ma_cross", _ma_cross_spec),
        ("rsi", _rsi_spec),
        ("donchian", _donchian_spec),
    ],
)
def test_runtime_signal_matches_vectorized_past_warmup(label, builder) -> None:
    """Past the warm-up gate, the runtime's per-bar signal == the vectorized value.

    The full-window vectorized ``signals`` is the backtest source of truth. Once the
    runtime is past its warm-up gate (and using a full buffer so EWM indicators are
    not truncated), every per-bar signal must equal the vectorized value -- the
    one-interpreter invariant. We start the comparison after the gate so this test is
    not confounded by the separate warm-up-gate suppression bug (which only affects
    the first few bars and is captured by its own BUG test below).
    """
    spec = builder()
    bars = make_bars(_sine(400))
    interp = SpecInterpreter(spec)
    vectorized = interp.signals(interp.prepare(df_from_bars(bars)))

    broker = PaperBroker(spec, ExecutionConfig())
    runtime = StrategyRuntime(spec, broker, buffer_size=len(bars) + 5)
    gate = runtime._min_signal_bars()

    mismatches: list[int] = []
    for i, bar in enumerate(bars):
        sig = runtime.on_bar(bar)
        if i < gate:
            continue
        got = (sig.enter_long, sig.enter_short, sig.exit_long, sig.exit_short)
        want = (
            vectorized[i].enter_long,
            vectorized[i].enter_short,
            vectorized[i].exit_long,
            vectorized[i].exit_short,
        )
        if got != want:
            mismatches.append(i)
    assert not mismatches, (
        f"{label}: runtime signal diverged from the vectorized engine past warm-up at "
        f"{len(mismatches)} bar(s); first at bar {mismatches[0]}"
    )


# --------------------------------------------------------------------------- #
# REGRESSION GUARDS for the two parity bugs QA found and the owning agents fixed.
# Each was once a confirmed INVARIANT-1 violation (live traded differently than
# backtest); both are now FIXED in src/ and asserted here as HARD 0-mismatch passes
# so any reintroduction fails the suite immediately.
#   * Bug #1: runtime warm-up gate over-suppressed valid early entries
#     (_min_signal_bars now = slowest + 1, admitting the first crossover bar).
#   * Bug #2: a bounded buffer truncated EWM indicator history (now the default
#     buffer is None == unbounded full prefix -> exact parity for RSI/EMA/MACD).
# --------------------------------------------------------------------------- #


def test_warmup_gate_admits_first_valid_entry() -> None:
    """The live runtime takes the SAME early entry the backtest takes (no suppression).

    Regression guard for the warm-up-gate over-suppression bug. Minimal case: an
    SMA(10) crossover whose FIRST ``enter_long`` lands at bar 10 (the SMA is defined
    at bar 9, the crossover one bar later). The runtime must admit that bar -- the
    backtest takes the trade, so the live path must too. 0 mismatch (same trade list
    + final equity).
    """
    spec = _ma_cross_spec(
        name="sma10-cross-early",
        indicators=[IndicatorSpec(id="sma10", kind="sma", params={"length": 10})],
        entry=EntryRules(long="crossover(close, sma10)"),
        exit=ExitRules(long="crossunder(close, sma10)"),
    )
    # Flat at 100 for bars 0..8; dip below the SMA at bar 9; jump above at bar 10 ->
    # the first crossover entry lands exactly at bar 10 (the formerly-suppressed bar).
    closes = [100.0] * 9 + [80.0, 300.0] + [300.0] * 15
    bars = make_bars(closes)
    # Sanity: the backtest actually takes a trade here (non-vacuous regression guard).
    assert len(run_backtest(spec, bars, config=_FRICTIONLESS).trades) >= 1
    _assert_zero(spec, bars, _FRICTIONLESS)


def test_default_buffer_ewm_indicator_parity() -> None:
    """RSI(14) on the runtime's DEFAULT (unbounded) buffer == the full-history backtest.

    Regression guard for the EWM-buffer-truncation bug. RSI uses Wilder/EWM smoothing
    whose value depends on the ENTIRE prior history, so the runtime's default buffer
    must retain the full prefix. With the fixed default (``buffer_size=None``), the
    streaming RSI reproduces the vectorized RSI exactly -> identical trades + equity.
    """
    spec = _rsi_spec()
    bars = make_bars(_sine(400))
    # Non-vacuous: the spec actually trades on this path.
    assert len(run_backtest(spec, bars, config=_FRICTIONLESS).trades) >= 1
    _assert_zero(spec, bars, _FRICTIONLESS, default_buffer=True)


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("rsi", _rsi_spec),
        ("ma_cross_ema", lambda **o: build_from_template("ma_cross", {"symbol": "BTC/USD", **o})),
        ("macd", lambda **o: build_from_template("macd", {"symbol": "BTC/USD", **o})),
    ],
)
def test_ewm_indicator_exact_parity_default_buffer(label, builder) -> None:
    """EWM indicators (RSI, EMA ma-cross, MACD) hit EXACT parity on the default buffer.

    Broadens the EWM regression guard across the EWM-based templates: each diverged
    under the old bounded buffer and now reproduces the backtest exactly under the
    unbounded full-prefix default. 0 mismatch (trade list + final equity).

    MACD is included: the prior crash (the runtime drove pandas-ta MACD over a
    too-short warm-up window, which returned ``None`` -> ``AttributeError`` in the
    indicator registry) is fixed at the root in
    :mod:`trader_mcp.indicators.registry` (a ``None`` pandas-ta result now degrades
    to NaN-filled output columns, i.e. NaN -> ``False`` during warm-up, parity-equal
    to the backtest). Every shipped MACD-template rule gates on the signal line
    ``crossover(m, m_signal)``, which is only defined well past the warm-up band, so
    the first possible signal lands where the runtime's growing prefix already yields
    real MACD values -- exact parity holds (verified: 21 trades, identical equity).
    """
    spec = builder()
    bars = make_bars(_sine(500))
    assert len(run_backtest(spec, bars, config=_FRICTIONLESS).trades) >= 1
    _assert_zero(spec, bars, _FRICTIONLESS, default_buffer=True)


def test_explicit_small_buffer_is_a_documented_approximation_for_ewm() -> None:
    """An EXPLICIT small ``buffer_size`` is the documented approximation, NOT exact parity.

    The contract: the DEFAULT (``None``) buffer is unbounded and exact; an explicit
    integer cap trades exactness for bounded memory and MAY diverge for EWM
    indicators (whose value needs the full history). We pin that contract by showing
    that a deliberately tiny trailing buffer on an EWM (RSI) spec diverges from the
    full-history backtest, while the default (unbounded) buffer does not. If a future
    change makes a small explicit buffer exact for EWM, this test fails loudly and the
    contract/docs must be revisited.
    """
    spec = _rsi_spec()
    bars = make_bars(_sine(400))
    report = run_backtest(spec, bars, config=_FRICTIONLESS)

    # Default (unbounded) buffer: exact parity.
    exact_broker = _stream(spec, bars, _FRICTIONLESS, default_buffer=True)
    assert abs(exact_broker.balance().cash - report.final_equity) < 1e-6

    # An explicit tiny trailing buffer (just past the RSI window) is an APPROXIMATION:
    # it truncates the EWM history, so it is expected to diverge from the backtest.
    approx_mismatches, _notes = _count_mismatches(spec, bars, _FRICTIONLESS, buffer_size=20)
    assert approx_mismatches > 0, (
        "an explicit small buffer was expected to APPROXIMATE (diverge) for an EWM "
        "indicator, but it matched exactly -- the bounded-buffer contract may have "
        "changed; revisit the runtime docs and this test"
    )
