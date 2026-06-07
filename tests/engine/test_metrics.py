"""Native metric formulas vs hand-computed fixtures."""

from __future__ import annotations

import math

from trader_mcp.engine import EquityPoint, compute_metrics, periods_per_year
from trader_mcp.engine.metrics import _max_drawdown_pct
from trader_mcp.engine.models import SimulatedTrade


def _curve(values: list[float]) -> list[EquityPoint]:
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [EquityPoint(timestamp=t0 + timedelta(hours=i), equity=v) for i, v in enumerate(values)]


def test_total_return_pct() -> None:
    m = compute_metrics(_curve([100, 110, 120]), [], initial_cash=100, timeframe="1h", total_bars=3)
    assert abs(m.total_return_pct - 20.0) < 1e-9


def test_max_drawdown_known() -> None:
    # Peak 120 -> trough 90 = 25% drawdown.
    assert abs(_max_drawdown_pct([100, 120, 90, 110]) - 25.0) < 1e-9


def test_max_drawdown_zero_when_monotonic() -> None:
    assert _max_drawdown_pct([100, 101, 102, 103]) == 0.0


def test_periods_per_year_for_1h() -> None:
    # 365 days * 24 h = 8760 hourly bars per year.
    assert abs(periods_per_year("1h") - 8760.0) < 1e-6
    assert abs(periods_per_year("1d") - 365.0) < 1e-6


def test_cagr_doubling_in_one_year_is_100pct() -> None:
    # 8760 hourly returns that double equity over exactly one year -> CAGR ~100%.
    n = 8760
    g = 2.0 ** (1 / n)  # per-bar growth to double in a year
    values = [100.0 * g**i for i in range(n + 1)]
    m = compute_metrics(_curve(values), [], initial_cash=100.0, timeframe="1h", total_bars=n + 1)
    assert abs(m.cagr_pct - 100.0) < 0.5


def test_sharpe_zero_when_flat() -> None:
    m = compute_metrics(
        _curve([100, 100, 100, 100]), [], initial_cash=100, timeframe="1h", total_bars=4
    )
    assert m.sharpe == 0.0
    assert m.volatility_pct == 0.0


def test_profit_factor_and_win_rate() -> None:
    from datetime import UTC, datetime

    t0 = datetime(2024, 1, 1, tzinfo=UTC)

    def trade(pnl: float) -> SimulatedTrade:
        return SimulatedTrade(
            side="long",
            entry_time=t0,
            exit_time=t0,
            entry_price=100,
            exit_price=100,
            size=1,
            pnl=pnl,
            pnl_pct=pnl,
            fees_paid=0,
            bars_held=1,
            exit_reason="signal",
        )

    trades = [trade(30), trade(-10), trade(20), trade(-10)]
    m = compute_metrics(_curve([100, 130]), trades, initial_cash=100, timeframe="1h", total_bars=10)
    assert m.win_rate_pct == 50.0
    # gross win 50, gross loss 20 -> PF 2.5
    assert abs(m.profit_factor - 2.5) < 1e-9
    assert m.trade_count == 4


def test_profit_factor_inf_when_no_losses() -> None:
    from datetime import UTC, datetime

    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    win = SimulatedTrade(
        side="long",
        entry_time=t0,
        exit_time=t0,
        entry_price=100,
        exit_price=110,
        size=1,
        pnl=10,
        pnl_pct=10,
        fees_paid=0,
        bars_held=1,
        exit_reason="signal",
    )
    m = compute_metrics(_curve([100, 110]), [win], initial_cash=100, timeframe="1h", total_bars=5)
    assert math.isinf(m.profit_factor)


def test_empty_inputs_are_well_defined() -> None:
    m = compute_metrics([], [], initial_cash=10_000, timeframe="1h", total_bars=0)
    assert m.total_return_pct == 0.0
    assert m.trade_count == 0
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
