"""Partial-fill handling (Phase 7 resilience): the LIVE-path execution atom.

A real exchange can fill one order in several pieces. We model each piece as a
typed :class:`FillEvent` and fold the cumulative pieces into an
:class:`OrderRecord` via :func:`aggregate_fills` -- the SAME accounting path the
paper/backtest broker uses for its single atomic fill. These tests pin:

* a single partial fill leaves a resting remainder with correct accounting;
* multiple cumulative partials roll up to the right average price and size and
  complete the order;
* volume-weighted average-price aggregation is exact;
* over-fill is impossible (a misbehaving feed cannot inflate the position);
* the paper simulation still fills atomically (open -> filled, remaining 0) so
  partial fills are purely a live concept and parity is untouched.

Determinism: fixed prices/quantities, no network, no RNG.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trader_mcp.execution import (
    ExecutionConfig,
    FillEvent,
    OrderIntent,
    PaperBroker,
    aggregate_fills,
)
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
)

from .conftest import make_bars

_T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _spec(**overrides) -> StrategySpec:
    base = {
        "name": "t",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "strategy_type": "rule",
        "indicators": [IndicatorSpec(id="fast", kind="sma", params={"length": 2})],
        "entry": EntryRules(long="close > fast"),
        "exit": ExitRules(long="close < fast"),
        "position_sizing": PositionSizing(mode="percent_equity", value=100),
        "fees": Fees(taker=0.001, maker=0.0),
    }
    base.update(overrides)
    return StrategySpec(**base)


# -- aggregate_fills (the shared accounting reducer) ---------------------------
def test_aggregate_no_fills_is_open() -> None:
    """No fills => nothing filled, full remainder, open."""
    filled, remaining, average, fee, status = aggregate_fills(amount=10.0, fills=[])
    assert (filled, remaining, average, fee, status) == (0.0, 10.0, None, 0.0, "open")


def test_aggregate_single_complete_fill_is_filled() -> None:
    """A single fill of the whole amount reduces to the atomic-fill result.

    This is the simulation's case: routing it through the aggregator changes no
    number (filled == amount, remaining 0, average == fill price, status filled).
    """
    fills = [FillEvent(qty=10.0, price=100.0, fee=1.0, timestamp=_T0)]
    filled, remaining, average, fee, status = aggregate_fills(amount=10.0, fills=fills)
    assert filled == 10.0
    assert remaining == 0.0
    assert average == 100.0
    assert fee == 1.0
    assert status == "filled"


def test_aggregate_single_partial_leaves_resting_remainder() -> None:
    """One partial fill: some filled, the rest still rests, status partially_filled."""
    fills = [FillEvent(qty=4.0, price=100.0, fee=0.4, timestamp=_T0)]
    filled, remaining, average, fee, status = aggregate_fills(amount=10.0, fills=fills)
    assert filled == 4.0
    assert remaining == 6.0
    assert average == 100.0
    assert fee == 0.4
    assert status == "partially_filled"


def test_aggregate_multiple_partials_complete_with_vwap() -> None:
    """Cumulative partials roll up to the volume-weighted average and complete."""
    fills = [
        FillEvent(qty=2.0, price=100.0, fee=0.2, timestamp=_T0),
        FillEvent(qty=3.0, price=110.0, fee=0.33, timestamp=_T0),
        FillEvent(qty=5.0, price=120.0, fee=0.6, timestamp=_T0),
    ]
    filled, remaining, average, fee, status = aggregate_fills(amount=10.0, fills=fills)
    assert filled == 10.0
    assert remaining == 0.0
    # VWAP = (2*100 + 3*110 + 5*120) / 10 = (200 + 330 + 600) / 10 = 113.0
    assert average is not None
    assert abs(average - 113.0) < 1e-12
    assert abs(fee - (0.2 + 0.33 + 0.6)) < 1e-12
    assert status == "filled"


def test_aggregate_partials_below_amount_stay_partial() -> None:
    """Two partials that do not reach the amount remain partially_filled."""
    fills = [
        FillEvent(qty=2.0, price=100.0, fee=0.2, timestamp=_T0),
        FillEvent(qty=3.0, price=110.0, fee=0.33, timestamp=_T0),
    ]
    filled, remaining, average, _fee, status = aggregate_fills(amount=10.0, fills=fills)
    assert filled == 5.0
    assert remaining == 5.0
    assert average is not None
    assert abs(average - (200.0 + 330.0) / 5.0) < 1e-12  # = 106.0
    assert status == "partially_filled"


def test_aggregate_overfill_is_clamped_to_amount() -> None:
    """A feed reporting MORE than the order size cannot inflate filled/position.

    Over-fill is impossible: cumulative filled is clamped to amount, remaining
    floored at zero, and the order reads as fully filled. (The VWAP still uses the
    raw reported quantities, but the position can never exceed what was ordered.)
    """
    fills = [
        FillEvent(qty=8.0, price=100.0, fee=0.8, timestamp=_T0),
        FillEvent(qty=5.0, price=100.0, fee=0.5, timestamp=_T0),  # would be 13 > 10
    ]
    filled, remaining, _average, _fee, status = aggregate_fills(amount=10.0, fills=fills)
    assert filled == 10.0  # clamped, NOT 13
    assert remaining == 0.0  # floored, never negative
    assert status == "filled"


# -- PaperBroker.apply_fills (live-path reconciliation entry point) ------------
def _open_order(broker: PaperBroker) -> str:
    rec = broker.submit(
        OrderIntent(symbol="BTC/USD", side="buy", amount=10.0, client_order_id="c1")
    )
    assert rec.status == "open"
    assert rec.remaining == 10.0
    return rec.order_id


def test_apply_fills_single_partial_updates_record() -> None:
    broker = PaperBroker(_spec(), ExecutionConfig())
    oid = _open_order(broker)
    updated = broker.apply_fills(oid, [FillEvent(qty=4.0, price=100.0, fee=0.4, timestamp=_T0)])
    assert updated is not None
    assert updated.status == "partially_filled"
    assert updated.filled == 4.0
    assert updated.remaining == 6.0
    assert updated.average == 100.0
    # The stored order reflects the update (not just the returned copy).
    stored = next(o for o in broker.order_history() if o.order_id == oid)
    assert stored.status == "partially_filled"
    assert stored.remaining == 6.0


def test_apply_fills_cumulative_partials_complete_order() -> None:
    broker = PaperBroker(_spec(), ExecutionConfig())
    oid = _open_order(broker)
    # Exchange reports the FULL cumulative fill list each time more arrives.
    broker.apply_fills(oid, [FillEvent(qty=4.0, price=100.0, fee=0.4, timestamp=_T0)])
    final = broker.apply_fills(
        oid,
        [
            FillEvent(qty=4.0, price=100.0, fee=0.4, timestamp=_T0),
            FillEvent(qty=6.0, price=105.0, fee=0.63, timestamp=_T0),
        ],
    )
    assert final is not None
    assert final.status == "filled"
    assert final.filled == 10.0
    assert final.remaining == 0.0
    # VWAP = (4*100 + 6*105) / 10 = (400 + 630)/10 = 103.0
    assert final.average is not None
    assert abs(final.average - 103.0) < 1e-12


def test_apply_fills_unknown_order_returns_none() -> None:
    broker = PaperBroker(_spec(), ExecutionConfig())
    assert broker.apply_fills("nope", [FillEvent(qty=1.0, price=1.0, timestamp=_T0)]) is None


def test_apply_fills_cannot_overfill_position_amount() -> None:
    broker = PaperBroker(_spec(), ExecutionConfig())
    oid = _open_order(broker)
    updated = broker.apply_fills(
        oid,
        [
            FillEvent(qty=7.0, price=100.0, fee=0.7, timestamp=_T0),
            FillEvent(qty=7.0, price=100.0, fee=0.7, timestamp=_T0),  # 14 > 10
        ],
    )
    assert updated is not None
    assert updated.filled == 10.0  # never exceeds the ordered amount
    assert updated.remaining == 0.0
    assert updated.status == "filled"


def test_partially_filled_order_can_be_canceled() -> None:
    """A partially-filled resting order is cancelable (cancels the remainder)."""
    broker = PaperBroker(_spec(), ExecutionConfig())
    oid = _open_order(broker)
    broker.apply_fills(oid, [FillEvent(qty=4.0, price=100.0, fee=0.4, timestamp=_T0)])
    assert broker.cancel(oid) is True
    stored = next(o for o in broker.order_history() if o.order_id == oid)
    assert stored.status == "canceled"


# -- simulation still fills atomically (parity is structurally untouched) ------
def test_simulated_fills_are_atomic_never_partial() -> None:
    """The paper simulation produces only open -> filled records (remaining 0).

    Partial fills are purely a LIVE-path concept; the in-memory simulation fills
    whole, so it never books a ``partially_filled`` order. This is WHY parity is
    preserved: the simulated net position the interpreter expects is unchanged.
    """
    spec = _spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000, slippage_pct=0.0))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 100.0, 98.0, 98.0], open_from_prev=False)

    broker.on_bar(bars[0])
    broker.submit(OrderIntent(symbol=spec.symbol, side="buy", amount=1.0, client_order_id="e"))
    broker.on_bar(bars[1])  # entry fills atomically
    broker.submit(
        OrderIntent(
            symbol=spec.symbol,
            side="sell",
            amount=1.0,
            client_order_id="x",
            reduce_only=True,
        )
    )
    broker.on_bar(bars[2])
    broker.on_bar(bars[3])  # close on the down bar
    broker.force_close()

    # No order this simulation ever produced is partially filled; every realized
    # fill record is fully filled with a zero remainder.
    statuses = [o.status for o in broker.order_history()]
    assert "partially_filled" not in statuses
    for o in broker.order_history():
        if o.status == "filled":
            assert o.remaining == 0.0
            assert o.filled == o.amount
