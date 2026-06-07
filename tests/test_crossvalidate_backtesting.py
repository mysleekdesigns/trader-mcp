"""Cross-validate the trader-mcp engine against backtesting.py (PRD §6 Phase 4 exit).

The custom event-driven interpreter is the source of truth, but a Phase 4 exit
criterion is that it AGREES with the independent reference engine ``backtesting.py``
on a clean rule strategy. This module runs the SAME synthetic bars through both and
asserts final equity / total return (and Sharpe) agree within a documented tolerance.

Config mapping (so the two engines model the same world)::

    trader-mcp                         backtesting.py
    ---------------------------------  --------------------------------------------
    decide on close(t), fill open(t+1) Backtest(..., trade_on_close=False)  [default]
    all fills are TAKER                commission = spec.fees.taker
    BacktestConfig.slippage_pct = 0    (no slippage knob; frictionless fill)
    position_sizing percent_equity v   self.buy(size=v/100)  (fraction of equity)
    SL/TP as % of entry                self.buy(sl=..., tp=...)  [omitted in the
                                       headline case to remove tie-break drift]
    risk-free rate = 0                 default

Residual drift we accept (and why it is small):
    * trader-mcp charges taker fees on BOTH legs as ``price*size*taker``;
      backtesting.py applies ``commission`` per fill the same way, but rounds share
      counts to whole units by default -- we pass ``exclusive_orders=True`` and a
      fractional ``size`` so the two size the position near-identically.
    * trader-mcp's conservative STOP-before-TP intrabar tie-break and warm-up NaN
      handling can shift a fill by a bar vs backtesting.py. The headline assertion
      uses a NO-stop/NO-tp long-only spec to eliminate that; a second test adds
      SL/TP with a looser tolerance to show they still track.

The whole thing is deterministic: synthetic bars are built from a fixed-seed numpy
Generator. ``backtesting`` is a DEV-only dep -- guarded with ``importorskip`` (same
pattern as the pandas-ta skips) so the suite still runs where it is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from trader_mcp.engine import BacktestConfig, periods_per_year, run_backtest
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

backtesting = pytest.importorskip("backtesting", reason="backtesting.py is a dev-only dependency")

_SEED = 4242
_N = 500
_FAST = 10
_SLOW = 30
_TAKER = 0.0006
_SIZE_PCT = 95.0  # near-full deployment so equity differences are not rounding dust


def _closes(n: int, *, seed: int = _SEED) -> np.ndarray:
    """A deterministic trending+oscillating price path (seeded numpy Generator)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    trend = 0.04 * t
    wave = 10.0 * np.sin(t / 22.0)
    noise = rng.normal(0.0, 1.0, size=n).cumsum() * 0.1
    return np.maximum(100.0 + trend + wave + noise, 1.0)


def _frames() -> tuple[list[OHLCVBar], pd.DataFrame]:
    """Build the SAME series as trader-mcp bars AND a backtesting.py OHLC frame.

    Gapless: each bar opens at the previous close (so the next-bar-open fill is
    well-defined and identical for both engines).
    """
    closes = _closes(_N)
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    rows: list[dict[str, float]] = []
    index: list[datetime] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        close = float(c)
        open_px = prev if i > 0 else close
        hi = max(open_px, close) * 1.002
        lo = min(open_px, close) * 0.998
        ts = t0 + timedelta(hours=i)
        bars.append(OHLCVBar(timestamp=ts, open=open_px, high=hi, low=lo, close=close, volume=1.0))
        rows.append({"Open": open_px, "High": hi, "Low": lo, "Close": close, "Volume": 1.0})
        # backtesting.py wants a naive DatetimeIndex.
        index.append(ts.replace(tzinfo=None))
        prev = close
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
    return bars, df


def _spec(*, stop: float | None = None, take: float | None = None) -> StrategySpec:
    """A LONG-ONLY fast/slow SMA cross spec, optionally with SL/TP percentages.

    SMA (not EMA) and long-only keep the backtesting.py analog trivial and exact.
    No short rules -- backtesting.py's default closes/opens are simplest long-only.
    """
    return StrategySpec(
        name="sma-cross-xval",
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
        risk=RiskLimits(stop_loss_pct=stop, take_profit_pct=take),
        fees=Fees(taker=_TAKER, maker=_TAKER),
    )


class _SmaCross(backtesting.Strategy):  # type: ignore[misc,name-defined]
    """backtesting.py mirror: long on fast>slow cross, flat on the reverse cross.

    Class attributes are set per-test (``_fast``/``_slow``/``_size``/``_sl``/``_tp``)
    so the strategy parameters match the trader-mcp spec exactly.
    """

    _fast = _FAST
    _slow = _SLOW
    _size = _SIZE_PCT / 100.0
    _sl: float | None = None
    _tp: float | None = None

    def init(self) -> None:
        from backtesting.lib import crossover

        self._cross = crossover
        close = pd.Series(self.data.Close)
        self.fast = self.I(lambda: np.asarray(close.rolling(self._fast).mean()))
        self.slow = self.I(lambda: np.asarray(close.rolling(self._slow).mean()))

    def next(self) -> None:
        if self._cross(self.fast, self.slow):
            if not self.position:
                kwargs: dict[str, float] = {"size": self._size}
                price = float(self.data.Close[-1])
                if self._sl is not None:
                    kwargs["sl"] = price * (1.0 - self._sl / 100.0)
                if self._tp is not None:
                    kwargs["tp"] = price * (1.0 + self._tp / 100.0)
                self.buy(**kwargs)
        elif self._cross(self.slow, self.fast) and self.position:
            self.position.close()


def _run_reference(
    df: pd.DataFrame, *, sl: float | None = None, tp: float | None = None
) -> tuple[float, float]:
    """Run backtesting.py with the matched config; return (final_equity, return_pct)."""
    _SmaCross._sl = sl
    _SmaCross._tp = tp
    bt = backtesting.Backtest(
        df,
        _SmaCross,
        cash=10_000,
        commission=_TAKER,
        trade_on_close=False,  # decide on close(t), fill open(t+1) -- the engine's rule
        exclusive_orders=True,
        finalize_trades=True,
    )
    stats = bt.run()
    return float(stats["Equity Final [$]"]), float(stats["Return [%]"])


# Documented tolerances (relative). The clean no-SL/TP case tracks tightly; the
# SL/TP case is looser because of the conservative stop-before-tp tie-break.
_REL_TOL_CLEAN = 0.01  # <= 1% relative on equity & return (the PRD target)
_REL_TOL_SLTP = 0.05


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-9)


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_crossvalidate_equity_and_return_clean() -> None:
    """No SL/TP, long-only SMA cross: trader-mcp == backtesting.py within <=1%.

    This is the headline Phase 4 cross-validation: with the fill timing, taker
    commission, zero slippage, and fractional sizing all matched, the two engines
    must agree on final equity and total return to within the documented 1%.
    """
    bars, df = _frames()
    report = run_backtest(_spec(), bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    ref_equity, ref_return = _run_reference(df)

    eq_rel = _rel(report.final_equity, ref_equity)
    ret_rel = _rel(report.metrics.total_return_pct, ref_return)

    assert report.metrics.trade_count > 0, "degenerate: no trades to compare"
    assert eq_rel <= _REL_TOL_CLEAN, (
        f"final equity drift {eq_rel:.4%} > {_REL_TOL_CLEAN:.0%}: "
        f"trader-mcp={report.final_equity:.2f} backtesting.py={ref_equity:.2f}"
    )
    assert ret_rel <= _REL_TOL_CLEAN, (
        f"total return drift {ret_rel:.4%} > {_REL_TOL_CLEAN:.0%}: "
        f"trader-mcp={report.metrics.total_return_pct:.4f}% "
        f"backtesting.py={ref_return:.4f}%"
    )


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_crossvalidate_sharpe_agrees() -> None:
    """Sharpe ratios agree once annualized on the SAME (per-bar) basis.

    The two engines do NOT report Sharpe on the same basis: trader-mcp annualizes
    per-bar (hourly) returns by ``periods_per_year('1h') = 8760``, whereas
    backtesting.py's ``stats['Sharpe Ratio']`` resamples to a coarser (daily/annual)
    basis -- so the raw numbers differ by ~sqrt(24) and are NOT comparable. To
    cross-validate apples-to-apples we recompute Sharpe from backtesting.py's OWN
    per-bar equity curve using the engine's exact formula (per-bar simple returns,
    sample std ddof=1, rf=0, x sqrt(periods_per_year)). On the identical equity path
    the two then agree tightly -- proof the engine's Sharpe formula is faithful.
    """
    bars, df = _frames()
    report = run_backtest(_spec(), bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    _SmaCross._sl = None
    _SmaCross._tp = None
    bt = backtesting.Backtest(
        df, _SmaCross, cash=10_000, commission=_TAKER, trade_on_close=False, exclusive_orders=True
    )
    stats = bt.run()

    equity = stats["_equity_curve"]["Equity"]
    rets = equity.pct_change().dropna().to_numpy()
    ppy = periods_per_year("1h")
    ref_sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(ppy))

    ours = report.metrics.sharpe
    assert _rel(ours, ref_sharpe) <= _REL_TOL_CLEAN, (
        f"Sharpe drift {_rel(ours, ref_sharpe):.4%} > {_REL_TOL_CLEAN:.0%} on a matched "
        f"per-bar annualization basis: trader-mcp={ours:.4f} "
        f"backtesting.py(recomputed)={ref_sharpe:.4f}"
    )


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_crossvalidate_with_stop_and_target() -> None:
    """With SL/TP added, the two engines still track within a looser tolerance.

    SL/TP introduces intrabar fills where trader-mcp's conservative stop-before-tp
    tie-break can differ from backtesting.py by a fill, so we widen the tolerance
    (documented) but still require the engines to agree to within 5% -- proof the
    risk-exit fill model is faithful, not just the plain signal path.
    """
    bars, df = _frames()
    spec = _spec(stop=2.0, take=4.0)
    report = run_backtest(spec, bars, config=BacktestConfig(initial_cash=10_000, slippage_pct=0))
    ref_equity, _ = _run_reference(df, sl=2.0, tp=4.0)

    eq_rel = _rel(report.final_equity, ref_equity)
    assert report.metrics.trade_count > 0
    assert eq_rel <= _REL_TOL_SLTP, (
        f"SL/TP equity drift {eq_rel:.4%} > {_REL_TOL_SLTP:.0%}: "
        f"trader-mcp={report.final_equity:.2f} backtesting.py={ref_equity:.2f}"
    )
