"""PaperBroker fill correctness: fees, sizing modes, SL/TP, funding, queries."""

from __future__ import annotations

from trader_mcp.execution import ExecutionConfig, OrderIntent, PaperBroker
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
)

from .conftest import make_bars


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


def _submit_buy(broker: PaperBroker, symbol: str) -> None:
    broker.submit(
        OrderIntent(symbol=symbol, side="buy", amount=1.0, client_order_id="c1", reason="signal")
    )


def test_market_buy_then_close_pays_taker_fee_both_legs() -> None:
    spec = _spec()
    cfg = ExecutionConfig(initial_cash=10_000, slippage_pct=0.0)
    broker = PaperBroker(spec, cfg)
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 100.0, 100.0], open_from_prev=False)

    broker.on_bar(bars[0])  # establish mark
    _submit_buy(broker, spec.symbol)  # pending entry
    broker.on_bar(bars[1])  # fill entry at open=100
    pos = broker.positions()[0]
    # percent_equity 100 of 10k at 100 => size ~ 100 (minus fee on equity? sizing uses equity).
    assert pos.side == "long"
    assert pos.entry_price == 100.0
    # entry fee = 100 * size * 0.001
    expected_size = 10_000.0 / 100.0  # equity-based notional / price
    assert abs(pos.size - expected_size) < 1e-9

    # Close it.
    broker.submit(
        OrderIntent(
            symbol=spec.symbol,
            side="sell",
            amount=pos.size,
            client_order_id="c2",
            reduce_only=True,
            reason="signal",
        )
    )
    broker.on_bar(bars[2])  # close at open=100
    trades = broker.trade_history()
    assert len(trades) == 1
    t = trades[0]
    entry_fee = 100.0 * expected_size * 0.001
    exit_fee = 100.0 * expected_size * 0.001
    assert abs(t.fees_paid - (entry_fee + exit_fee)) < 1e-9
    # Flat price => pnl is just the two fees, negative.
    assert abs(t.pnl - (-(entry_fee + exit_fee))) < 1e-9


def test_fixed_base_sizing() -> None:
    spec = _spec(position_sizing=PositionSizing(mode="fixed_base", value=0.5))
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([200.0, 200.0, 200.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _submit_buy(broker, spec.symbol)
    broker.on_bar(bars[1])
    assert broker.positions()[0].size == 0.5


def test_fixed_quote_sizing() -> None:
    spec = _spec(position_sizing=PositionSizing(mode="fixed_quote", value=1_000.0))
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 100.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _submit_buy(broker, spec.symbol)
    broker.on_bar(bars[1])
    assert abs(broker.positions()[0].size - 10.0) < 1e-9  # 1000 quote / 100 price


def test_stop_loss_fires_intrabar() -> None:
    spec = _spec(risk=RiskLimits(stop_loss_pct=5.0))
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    # Enter at 100, then a bar that dips to 90 (below the 95 stop).
    bars = [
        *make_bars([100.0, 100.0], open_from_prev=False),
    ]
    # Manually craft a bar with a low that breaches the stop.
    from trader_mcp.exchanges.models import OHLCVBar

    breach = OHLCVBar(
        timestamp=bars[-1].timestamp,
        open=100.0,
        high=100.0,
        low=90.0,
        close=92.0,
        volume=1.0,
    )
    broker.on_bar(bars[0])
    _submit_buy(broker, spec.symbol)
    broker.on_bar(bars[1])  # fill entry at open 100
    assert broker.positions()  # open
    broker.on_bar(breach)  # SL at 95 should fire intrabar
    trades = broker.trade_history()
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_price == 95.0


def test_take_profit_fires_intrabar() -> None:
    spec = _spec(risk=RiskLimits(take_profit_pct=5.0))
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    from trader_mcp.exchanges.models import OHLCVBar

    bars = make_bars([100.0, 100.0], open_from_prev=False)
    breach = OHLCVBar(
        timestamp=bars[-1].timestamp,
        open=100.0,
        high=110.0,
        low=100.0,
        close=108.0,
        volume=1.0,
    )
    broker.on_bar(bars[0])
    _submit_buy(broker, spec.symbol)
    broker.on_bar(bars[1])
    broker.on_bar(breach)
    trades = broker.trade_history()
    assert len(trades) == 1
    assert trades[0].exit_reason == "take_profit"
    assert trades[0].exit_price == 105.0


def test_perp_funding_charged_on_long() -> None:
    spec = _spec(
        symbol="BTC/USDT:USDT",
        risk=RiskLimits(max_leverage=2),
    )
    cfg = ExecutionConfig(
        initial_cash=10_000, funding_enabled=True, funding_rate=0.001, funding_interval_hours=8.0
    )
    broker = PaperBroker(spec, cfg)
    broker.set_bar_hours(1.0)
    # Hold for 10 bars (>8h => one funding charge) at flat price.
    bars = make_bars([100.0] * 12, open_from_prev=False)
    broker.on_bar(bars[0])
    _submit_buy(broker, spec.symbol)
    broker.on_bar(bars[1])  # entry
    for b in bars[2:]:
        broker.on_bar(b)
    pos = broker.positions()[0]
    assert pos.funding_paid > 0.0  # a long pays positive funding


def test_cancel_open_order() -> None:
    spec = _spec()
    broker = PaperBroker(spec, ExecutionConfig())
    broker.set_bar_hours(1.0)
    rec = broker.submit(
        OrderIntent(symbol=spec.symbol, side="buy", amount=1.0, client_order_id="c1")
    )
    assert rec.status == "open"
    assert broker.cancel(rec.order_id) is True
    assert broker.cancel(rec.order_id) is False  # already canceled
    assert all(o.status != "open" for o in broker.order_history())


def test_portfolio_and_pnl_snapshots() -> None:
    spec = _spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 110.0], open_from_prev=False)
    broker.on_bar(bars[0])
    _submit_buy(broker, spec.symbol)
    broker.on_bar(bars[1])  # entry at 100
    broker.on_bar(bars[2])  # mark to 110
    port = broker.portfolio()
    pnl = broker.pnl()
    assert port.mark_price == 110.0
    assert port.position is not None
    assert pnl.unrealized > 0.0  # price rose with an open long
    assert abs(port.equity - (port.cash + port.position_value)) < 1e-6
