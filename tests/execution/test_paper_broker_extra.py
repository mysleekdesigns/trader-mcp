"""Extra PaperBroker coverage: PnL decomposition, short funding, grid/DCA portfolio.

Complements ``test_paper_broker.py`` (fees, sizing, intrabar SL/TP, long funding,
cancel, basic portfolio) with the parts that gate the portfolio/analytics tools:

  * the PnLBreakdown decomposition (realized/unrealized/fees/funding/total/return),
  * short-side perp funding (a short RECEIVES funding -> negative funding_paid),
  * mark-to-market for an open SHORT,
  * grid + DCA balance() holdings and portfolio() position_value,
  * the simulated=True invariant on every produced OrderRecord.

Deterministic and offline: bars are built from fixed closes, never wall-clock.
"""

from __future__ import annotations

from trader_mcp.execution import ExecutionConfig, OrderIntent, PaperBroker
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


def _rule_spec(**overrides) -> StrategySpec:
    base = {
        "name": "t",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "strategy_type": "rule",
        "indicators": [IndicatorSpec(id="fast", kind="sma", params={"length": 2})],
        "entry": EntryRules(long="close > fast", short="close < fast"),
        "exit": ExitRules(long="close < fast", short="close > fast"),
        "position_sizing": PositionSizing(mode="percent_equity", value=100),
        "fees": Fees(taker=0.001, maker=0.0),
    }
    base.update(overrides)
    return StrategySpec(**base)


def _buy(broker: PaperBroker, symbol: str, coid: str = "c1") -> None:
    broker.submit(OrderIntent(symbol=symbol, side="buy", amount=1.0, client_order_id=coid))


def _sell_short(broker: PaperBroker, symbol: str, coid: str = "s1") -> None:
    broker.submit(OrderIntent(symbol=symbol, side="sell", amount=1.0, client_order_id=coid))


# --------------------------------------------------------------------------- #
# PnL decomposition
# --------------------------------------------------------------------------- #
def test_pnl_breakdown_decomposes_realized_unrealized_fees() -> None:
    """total == realized + unrealized; fees accumulate; return_pct is over initial cash."""
    spec = _rule_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 120.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _buy(broker, spec.symbol)
    broker.on_bar(bars[1])  # entry at 100
    broker.on_bar(bars[2])  # mark to 120 with an OPEN long
    pnl = broker.pnl()
    assert pnl.realized == 0.0  # nothing closed yet
    assert pnl.unrealized > 0.0  # the open long is up
    assert pnl.fees_paid > 0.0  # entry taker fee paid
    assert abs(pnl.total - (pnl.realized + pnl.unrealized)) < 1e-9
    # return_pct is total over initial cash.
    assert abs(pnl.return_pct - (pnl.total / 10_000.0 * 100.0)) < 1e-9


def test_pnl_realized_after_close_matches_trade() -> None:
    """Once the position closes, realized PnL equals the closed trade's PnL."""
    spec = _rule_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 110.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _buy(broker, spec.symbol)
    broker.on_bar(bars[1])  # entry at 100
    broker.submit(
        OrderIntent(
            symbol=spec.symbol, side="sell", amount=1.0, client_order_id="x", reduce_only=True
        )
    )
    broker.on_bar(bars[2])  # exit at open 110
    trades = broker.trade_history()
    assert len(trades) == 1
    pnl = broker.pnl()
    assert pnl.unrealized == 0.0  # flat
    assert abs(pnl.realized - trades[0].pnl) < 1e-9
    assert abs(pnl.total - trades[0].pnl) < 1e-9


# --------------------------------------------------------------------------- #
# Short-side mark-to-market + funding
# --------------------------------------------------------------------------- #
def test_short_position_marks_to_market() -> None:
    """An open short gains as price falls; portfolio/pnl reflect the unrealized gain."""
    spec = _rule_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 90.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _sell_short(broker, spec.symbol)
    broker.on_bar(bars[1])  # short entry at 100
    broker.on_bar(bars[2])  # mark to 90 -> short is up
    pos = broker.positions()[0]
    assert pos.side == "short"
    assert broker.pnl().unrealized > 0.0
    port = broker.portfolio()
    assert port.mark_price == 90.0
    assert abs(port.equity - (port.cash + port.position_value)) < 1e-6


def test_short_perp_receives_funding() -> None:
    """A SHORT perp position RECEIVES funding -> funding_paid is negative."""
    spec = _rule_spec(symbol="BTC/USDT:USDT", risk=RiskLimits(max_leverage=2))
    cfg = ExecutionConfig(
        initial_cash=10_000, funding_enabled=True, funding_rate=0.001, funding_interval_hours=8.0
    )
    broker = PaperBroker(spec, cfg)
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0] * 12, open_from_prev=False)
    broker.on_bar(bars[0])
    _sell_short(broker, spec.symbol)
    broker.on_bar(bars[1])  # short entry
    for b in bars[2:]:
        broker.on_bar(b)
    pos = broker.positions()[0]
    assert pos.side == "short"
    assert pos.funding_paid < 0.0  # a short is paid funding (negative cost)


# --------------------------------------------------------------------------- #
# Grid + DCA balance / portfolio
# --------------------------------------------------------------------------- #
def _grid_spec() -> StrategySpec:
    return StrategySpec(
        name="grid",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="grid",
        grid=GridConfig(lower=90.0, upper=110.0, levels=5, allocation_pct=50.0),
        position_sizing=PositionSizing(mode="percent_equity", value=50.0),
        fees=Fees(taker=0.001),
    )


def _dca_spec() -> StrategySpec:
    return StrategySpec(
        name="dca",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="dca",
        dca=DCAConfig(amount_quote=100.0, interval_bars=2, max_purchases=None),
        position_sizing=PositionSizing(mode="fixed_quote", value=100.0),
        fees=Fees(taker=0.001),
    )


def test_grid_balance_reports_held_base_and_portfolio_marks() -> None:
    """Grid: after a level fills, balance().holdings carries base and equity marks it."""
    spec = _grid_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000, slippage_pct=0.0))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 95.0, 96.0], open_from_prev=False)
    broker.on_bar(bars[0])
    # Submit a grid buy at a level the next bar can fill.
    broker.submit(
        OrderIntent(
            symbol=spec.symbol,
            side="buy",
            type="limit",
            amount=1.0,
            price=95.0,
            client_order_id="g1",
            reason="grid",
        )
    )
    broker.on_bar(bars[1])  # fills the 95 level
    broker.on_bar(bars[2])  # mark
    bal = broker.balance()
    base = "BTC"
    assert bal.holdings.get(base, 0.0) > 0.0  # base accumulated
    port = broker.portfolio()
    assert port.position_value > 0.0  # grid inventory marked to market
    # Every produced order record is a simulated record.
    assert all(o.simulated for o in broker.order_history())


def test_dca_balance_accumulates_base_over_purchases() -> None:
    """DCA: each scheduled buy accumulates base; balance/portfolio reflect it."""
    spec = _dca_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000, slippage_pct=0.0))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 100.0, 100.0], open_from_prev=False)
    broker.on_bar(bars[0])
    for i, b in enumerate(bars[1:], start=1):
        broker.submit(
            OrderIntent(
                symbol=spec.symbol,
                side="buy",
                amount=1.0,
                client_order_id=f"d{i}",
                reason="dca",
            )
        )
        broker.on_bar(b)
    bal = broker.balance()
    assert bal.holdings.get("BTC", 0.0) > 0.0
    assert bal.cash < 10_000.0  # spent quote on purchases
    port = broker.portfolio()
    assert port.position_value > 0.0


# --------------------------------------------------------------------------- #
# simulated invariant + force-close accounting
# --------------------------------------------------------------------------- #
def test_every_order_record_is_simulated() -> None:
    """SAFETY: a PaperBroker NEVER emits a non-simulated order record."""
    spec = _rule_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 110.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _buy(broker, spec.symbol)
    broker.on_bar(bars[1])
    broker.submit(
        OrderIntent(
            symbol=spec.symbol, side="sell", amount=1.0, client_order_id="x", reduce_only=True
        )
    )
    broker.on_bar(bars[2])
    assert broker.order_history()  # non-empty
    assert all(o.simulated is True for o in broker.order_history())


def test_force_close_flattens_and_records_terminal_trade() -> None:
    """force_close() closes an open position at the last mark and records the trade."""
    spec = _rule_spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 130.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _buy(broker, spec.symbol)
    broker.on_bar(bars[1])  # entry at 100
    broker.on_bar(bars[2])  # mark to 130, still open
    assert broker.positions()  # open
    broker.force_close()
    assert not broker.positions()  # flat
    trades = broker.trade_history()
    assert len(trades) == 1
    assert trades[0].exit_reason == "end_of_data"
    # Realized pnl is positive (closed a winning long).
    assert broker.pnl().realized > 0.0
