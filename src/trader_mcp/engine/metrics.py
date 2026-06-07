"""Native performance metrics computed from an equity curve + trades (PRD §6 Phase 4).

INVARIANT: every headline metric is computed here with numpy/pandas ONLY -- no
quantstats dependency. quantstats is used solely for the optional HTML tear sheet
(:mod:`trader_mcp.engine.tearsheet`); the numbers the engine reports and that QA
cross-validates are these, so they must be self-contained and explicit.

Formulas & assumptions (documented loudly so cross-validation can match them):

    Period returns
        ``r_t = equity_t / equity_{t-1} - 1`` over the per-bar equity curve. All
        ratio metrics are computed on these per-bar returns then annualized by the
        number of bars per year (derived from the timeframe via
        :func:`trader_mcp.data.timeframes.timeframe_ms`).

    Total return
        ``final / initial - 1`` (percent).

    CAGR (annualized return)
        ``(final / initial) ** (periods_per_year / n_bars) - 1`` (percent). With
        ``n_bars`` the number of return observations. Geometric, calendar-agnostic
        (uses bar cadence, not wall-clock), so a fixed data window is reproducible.

    Volatility (annualized)
        ``std(r, ddof=1) * sqrt(periods_per_year)`` (percent). Sample std (ddof=1);
        a series with < 2 returns has volatility 0.

    Sharpe (annualized)
        ``mean(r) / std(r, ddof=1) * sqrt(periods_per_year)``. RISK-FREE RATE IS
        ZERO (documented assumption -- crypto, short horizons; backtesting.py's
        default is likewise rf=0). Undefined (zero std) -> 0.0.

    Sortino (annualized)
        ``mean(r) / downside_std(r) * sqrt(periods_per_year)`` where downside std
        uses only negative returns (ddof=1, rf=0). No downside -> 0.0.

    Max drawdown
        Largest peak-to-trough decline of the equity curve, as a positive percent
        (``max(1 - equity / running_peak)``).

    Calmar
        ``cagr / max_drawdown`` (both as fractions). No drawdown -> 0.0.

    Win rate
        ``wins / trade_count`` (percent); a trade wins when ``pnl > 0``.

    Profit factor
        ``sum(winning pnl) / abs(sum(losing pnl))``. No losses but some wins ->
        ``inf``; no trades -> 0.0.

    Average trade
        Mean of per-trade ``pnl_pct`` (percent).

    Exposure
        Fraction of bars during which a position was open (percent). Computed from
        trade spans; for the bar-level equity engine this is
        ``bars_in_market / total_bars``.

    Trade count
        Number of closed round-trip trades.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.engine.models import BacktestMetrics

if TYPE_CHECKING:
    from trader_mcp.engine.models import EquityPoint, SimulatedTrade

#: Milliseconds in a 365-day year -- the annualization base for CAGR/Sharpe so the
#: result is calendar-agnostic (driven by bar cadence, reproducible on fixed data).
_YEAR_MS = 365 * 24 * 60 * 60 * 1000


def periods_per_year(timeframe: str) -> float:
    """Return how many bars of ``timeframe`` fit in a 365-day year (annualization)."""
    return _YEAR_MS / timeframe_ms(timeframe)


def compute_metrics(
    equity_curve: list[EquityPoint],
    trades: list[SimulatedTrade],
    *,
    initial_cash: float,
    timeframe: str,
    total_bars: int,
) -> BacktestMetrics:
    """Compute the full :class:`BacktestMetrics` natively from curve + trades.

    Args:
        equity_curve: Per-bar mark-to-market equity samples (ascending in time).
        trades: Closed round-trip trades.
        initial_cash: Starting equity (curve may start below this after fees).
        timeframe: The bar cadence key, for annualization.
        total_bars: The number of bars the backtest ran over (for exposure).

    Returns:
        A :class:`BacktestMetrics`. Empty/degenerate inputs yield an all-zero,
        well-defined result (never NaN/inf except ``profit_factor``).
    """
    import numpy as np

    equities = [pt.equity for pt in equity_curve]
    final = equities[-1] if equities else initial_cash
    total_return_pct = (final / initial_cash - 1.0) * 100.0 if initial_cash > 0 else 0.0

    # Per-bar simple returns off the equity curve.
    eq = np.asarray(equities, dtype="float64")
    if eq.size >= 2:
        rets = eq[1:] / eq[:-1] - 1.0
        rets = rets[np.isfinite(rets)]
    else:
        rets = np.asarray([], dtype="float64")

    ppy = periods_per_year(timeframe)
    n_rets = int(rets.size)

    # CAGR (geometric, annualized by bar count). Computed via logs and clamped so
    # an extreme exponent on a very short window (e.g. annualizing 2 bars) cannot
    # overflow -- a tiny window produces a huge-but-finite, clearly-unreliable CAGR
    # rather than crashing. The clamp bound (+/- 1e6 %) is well beyond any
    # meaningful result and exists purely to keep the number finite.
    cagr_pct = 0.0
    if n_rets >= 1 and initial_cash > 0 and final > 0:
        log_growth = math.log(final / initial_cash) * (ppy / n_rets)
        log_growth = max(min(log_growth, 50.0), -50.0)
        cagr_pct = (math.exp(log_growth) - 1.0) * 100.0

    # Volatility / Sharpe / Sortino (rf = 0).
    if n_rets >= 2:
        std = float(np.std(rets, ddof=1))
        mean = float(np.mean(rets))
        volatility_pct = std * math.sqrt(ppy) * 100.0
        sharpe = (mean / std * math.sqrt(ppy)) if std > 0 else 0.0
        downside = rets[rets < 0]
        if downside.size >= 1:
            dstd = (
                float(np.std(downside, ddof=1)) if downside.size >= 2 else float(abs(downside[0]))
            )
            sortino = (mean / dstd * math.sqrt(ppy)) if dstd > 0 else 0.0
        else:
            sortino = 0.0
    else:
        volatility_pct = sharpe = sortino = 0.0

    # Max drawdown off the equity curve.
    max_dd_pct = _max_drawdown_pct(eq)

    # Calmar = CAGR / MaxDD (both fractions).
    calmar = (cagr_pct / max_dd_pct) if max_dd_pct > 0 else 0.0

    # Trade-derived metrics.
    trade_count = len(trades)
    if trade_count > 0:
        wins = [t for t in trades if t.pnl > 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = sum(t.pnl for t in trades if t.pnl < 0)
        win_rate_pct = len(wins) / trade_count * 100.0
        if gross_loss < 0:
            profit_factor = gross_win / abs(gross_loss)
        elif gross_win > 0:
            profit_factor = math.inf
        else:
            profit_factor = 0.0
        avg_trade_pct = sum(t.pnl_pct for t in trades) / trade_count
        bars_in_market = sum(max(t.bars_held, 0) for t in trades)
        exposure_pct = (bars_in_market / total_bars * 100.0) if total_bars > 0 else 0.0
        exposure_pct = min(exposure_pct, 100.0)
    else:
        win_rate_pct = profit_factor = avg_trade_pct = exposure_pct = 0.0

    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        volatility_pct=volatility_pct,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown_pct=max_dd_pct,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        avg_trade_pct=avg_trade_pct,
        exposure_pct=exposure_pct,
        trade_count=trade_count,
    )


def _max_drawdown_pct(equities: object) -> float:
    """Largest peak-to-trough decline of the curve, as a positive percent."""
    import numpy as np

    eq = np.asarray(equities, dtype="float64")
    if eq.size == 0:
        return 0.0
    running_peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(running_peak > 0, 1.0 - eq / running_peak, 0.0)
    max_dd = float(np.max(drawdowns)) if drawdowns.size else 0.0
    return max(0.0, max_dd) * 100.0
