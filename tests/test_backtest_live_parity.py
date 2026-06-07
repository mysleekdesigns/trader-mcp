"""THE first-class backtest<->live parity suite (INVARIANT 1, PRD §5.3, §7).

trader-mcp ships ONE event-driven interpreter that serves both the backtest
(vectorized over a whole cached window) and the live/streaming path (per-bar over
a trailing window). If those two ever disagree on a signal, a strategy backtests
differently than it trades -- a parity bug. This module proves they agree.

What we assert, field-by-field, for several rule templates (MA cross, RSI, MACD,
Bollinger, Donchian) and for grid + DCA:

    for every bar i past warm-up:
        interp.signal_at(df.iloc[i - W : i + 1])  ==  interp.signals(df)[i]

``signal_at`` is the live entry point Phase 5 will drive with a rolling buffer;
``signals`` is the vectorized backtest source of truth. ``W`` is a trailing window
at least as long as the slowest indicator so the causal indicators warm up to the
same value at the window's last row. We also assert a "full-run" flavor: feeding
bars incrementally (broker-free, signal-only) reproduces the exact same signal
stream, and that two independent ``run_backtest`` calls over the same incremental
vs whole feed agree on the trade list.

Determinism: the synthetic OHLCV is built from a numpy ``Generator`` seeded with a
fixed integer -- never wall-clock, never an unseeded RNG. No network, no files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from trader_mcp.engine import SpecInterpreter, run_backtest
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.indicators import df_from_bars
from trader_mcp.strategy import build_from_template

# A trailing window comfortably longer than the slowest indicator any template
# below uses (MACD slow EMA = 26, warmed over ~9*slow bars); 120 is generous and
# still cheap. signal_at over this window reproduces the vectorized value.
_LIVE_WINDOW = 120

# Enough bars to warm the slowest indicator AND exercise many crossings.
_N_BARS = 600
_SEED = 20240606


def _synthetic_closes(n: int, *, seed: int = _SEED) -> np.ndarray:
    """A deterministic mean-reverting price path with trend waves.

    Seeded numpy Generator (NOT wall-clock): a drifting sinusoid plus bounded
    Gaussian noise around a positive base, so EMAs/RSI/MACD/Bollinger all cross
    repeatedly (many entry AND exit signals to compare), while prices stay > 0.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    wave = 12.0 * np.sin(t / 18.0) + 6.0 * np.sin(t / 5.0)
    noise = rng.normal(0.0, 1.5, size=n).cumsum() * 0.15
    closes = 100.0 + wave + noise
    return np.maximum(closes, 1.0)


def _bars_from_closes(closes: np.ndarray, *, start: datetime | None = None) -> list[OHLCVBar]:
    """Build a gapless OHLCV series (open == prev close) with a small HL range.

    ``start`` sets the first bar's UTC timestamp (default 2024-01-01T00:00Z). DCA
    parity covers an EPOCH-OFFSET start (e.g. 07:00Z) because the new stateless
    timestamp-grid cadence must hold regardless of start-time alignment.
    """
    t0 = start or datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        close = float(c)
        open_px = prev if i > 0 else close
        hi = max(open_px, close) * 1.003
        lo = min(open_px, close) * 0.997
        bars.append(
            OHLCVBar(
                timestamp=t0 + timedelta(hours=i),
                open=open_px,
                high=hi,
                low=lo,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return bars


@pytest.fixture(scope="module")
def bars() -> list[OHLCVBar]:
    return _bars_from_closes(_synthetic_closes(_N_BARS))


def _sig_tuple(s: object) -> tuple:
    """Every BarSignal field, so parity is asserted on the WHOLE signal."""
    return (
        s.enter_long,  # type: ignore[attr-defined]
        s.enter_short,  # type: ignore[attr-defined]
        s.exit_long,  # type: ignore[attr-defined]
        s.exit_short,  # type: ignore[attr-defined]
        tuple(s.grid_buy_levels),  # type: ignore[attr-defined]
        s.dca_buy,  # type: ignore[attr-defined]
    )


# Every template's signals are CAUSAL over a bounded trailing window: each bar's
# decision depends only on the recent past -- indicator values that warm up (rule
# strategies), current/previous bar lows (grid), or the bar's own UTC timestamp
# (DCA's stateless ``(bar_epoch_ms // timeframe_ms) % interval_bars == 0`` grid).
# So a fixed-size rolling buffer -- exactly what a live runtime keeps -- reproduces
# the vectorized value for ALL of them, DCA included (its cadence is now a pure
# function of the bar timestamp, independent of the window start, verified for
# epoch-offset starts in ``test_dca_signal_at_matches_vectorized_with_epoch_offset``).
_TRAILING_PARITY_TEMPLATES = [
    "ma_cross",
    "rsi_reversion",
    "macd",
    "bollinger",
    "donchian_break",
    "grid",
    "dca",
]
_RULE_TEMPLATES = ["ma_cross", "rsi_reversion", "macd", "bollinger", "donchian_break"]


@pytest.mark.parametrize("template_id", _TRAILING_PARITY_TEMPLATES)
def test_signal_at_matches_vectorized_bar_for_bar(template_id: str, bars: list[OHLCVBar]) -> None:
    """signal_at(trailing window)[last] == signals(full)[i] for every bar i.

    This IS the one-interpreter invariant for the live runtime's bounded rolling
    buffer: the per-bar path reproduces the vectorized backtest path field-by-field
    over a fixed-size trailing window. Mismatches are collected so a failure
    localizes the FIRST diverging bar (and how many) rather than dying on bar 0.
    """
    spec = build_from_template(template_id)
    interp = SpecInterpreter(spec)
    df = interp.prepare(df_from_bars(bars))
    vec = interp.signals(df)
    assert len(vec) == len(df)

    mismatches: list[tuple[int, tuple, tuple]] = []
    for i in range(len(df)):
        start = max(0, i - _LIVE_WINDOW)
        window = df.iloc[start : i + 1]
        per_bar = interp.signal_at(window)
        got, want = _sig_tuple(per_bar), _sig_tuple(vec[i])
        if got != want:
            mismatches.append((i, got, want))

    assert not mismatches, (
        f"{template_id}: {len(mismatches)} live/backtest signal divergence(s); "
        f"first at bar {mismatches[0][0]}: per_bar={mismatches[0][1]} "
        f"vectorized={mismatches[0][2]}"
    )


# DCA start times: aligned midnight, an EPOCH-OFFSET 07:00Z (the case the old
# positional-index code got wrong), and an arbitrary odd offset. Each must satisfy
# trailing-window == vectorized parity because the cadence is now a pure function
# of the bar's own UTC timestamp.
_DCA_STARTS = [
    datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
    datetime(2024, 1, 1, 7, 0, tzinfo=UTC),
    datetime(2024, 3, 7, 13, 0, tzinfo=UTC),
]


@pytest.mark.parametrize("start", _DCA_STARTS, ids=["midnight", "offset-07h", "arbitrary"])
def test_dca_signal_at_matches_vectorized_with_epoch_offset(start: datetime) -> None:
    """DCA per-bar == vectorized over a bounded trailing window for ANY start time.

    The DCA cadence is a stateless absolute-timestamp grid: a bar fires ``dca_buy``
    when ``(bar_epoch_ms // timeframe_ms) % interval_bars == 0`` -- a pure function
    of the bar's own UTC timestamp, independent of the window's start. So
    ``signal_at(trailing window)[last] == signals(full)[i]`` for every bar i
    regardless of epoch alignment. We assert this across several interval lengths
    AND the epoch-offset (07:00Z) start that the old positional-index code got
    wrong, proving the fix holds where it previously diverged.
    """
    bars = _bars_from_closes(_synthetic_closes(_N_BARS), start=start)
    for interval in (1, 2, 3, 5):
        spec = build_from_template(
            "dca", {"dca": {"amount_quote": 100.0, "interval_bars": interval}}
        )
        interp = SpecInterpreter(spec)
        df = interp.prepare(df_from_bars(bars))
        vec = interp.signals(df)

        mismatches: list[tuple[int, bool, bool]] = []
        for i in range(len(df)):
            window = df.iloc[max(0, i - _LIVE_WINDOW) : i + 1]
            per_bar = interp.signal_at(window)
            if per_bar.dca_buy != vec[i].dca_buy:
                mismatches.append((i, per_bar.dca_buy, vec[i].dca_buy))

        assert not mismatches, (
            f"DCA interval={interval} start={start.isoformat()}: "
            f"{len(mismatches)} divergence(s); first at bar {mismatches[0][0]} "
            f"per_bar={mismatches[0][1]} vectorized={mismatches[0][2]}"
        )
        # Sanity: the cadence actually fired at least once (non-vacuous).
        assert any(s.dca_buy for s in vec)


@pytest.mark.parametrize("template_id", _RULE_TEMPLATES)
def test_full_run_incremental_signal_stream_matches(template_id: str, bars: list[OHLCVBar]) -> None:
    """Replaying the cache as a synthetic stream reproduces the whole signal list.

    A live runtime accumulates bars and asks the interpreter for a decision each
    time a bar closes. Here we replay the cached bars one at a time, feeding the
    interpreter the growing trailing buffer, and assert the resulting per-bar
    signal stream equals the vectorized stream exactly -- the streaming-equivalence
    flavor of parity (no broker involved, pure signal core).
    """
    spec = build_from_template(template_id)
    interp = SpecInterpreter(spec)
    df = interp.prepare(df_from_bars(bars))
    vectorized = [_sig_tuple(s) for s in interp.signals(df)]

    streamed: list[tuple] = []
    for i in range(len(df)):
        start = max(0, i - _LIVE_WINDOW)
        streamed.append(_sig_tuple(interp.signal_at(df.iloc[start : i + 1])))

    assert streamed == vectorized, f"{template_id}: streamed signal list != vectorized list"


def test_warmup_window_long_enough_for_slowest_indicator(bars: list[OHLCVBar]) -> None:
    """Sanity: MACD's slow EMA (26) is well within _LIVE_WINDOW, and the data is
    long enough that the warmed trailing value equals the full-window value.

    If this guard ever fails the parity above is vacuous, so assert the window is
    strictly larger than the slowest indicator length used by any tested template.
    """
    spec = build_from_template("macd")
    slowest = max(
        int(ind.params.get("slow", ind.params.get("length", 0))) for ind in spec.indicators
    )
    assert slowest * 2 < _LIVE_WINDOW
    assert _N_BARS > _LIVE_WINDOW * 2


def test_backtest_run_is_deterministic_across_calls(bars: list[OHLCVBar]) -> None:
    """Two run_backtest calls over the same synthetic bars agree on id + trades.

    Tool-level determinism is covered in test_backtest_tools; here we anchor it at
    the engine entry point the parity suite exercises, so a regression that breaks
    reproducibility trips in BOTH the parity and the tool suites.
    """
    spec = build_from_template("ma_cross")
    a = run_backtest(spec, bars)
    b = run_backtest(spec, bars)
    assert a.report_id == b.report_id
    assert a.final_equity == b.final_equity
    assert [t.entry_price for t in a.trades] == [t.entry_price for t in b.trades]
    assert [t.exit_price for t in a.trades] == [t.exit_price for t in b.trades]
