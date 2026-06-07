"""Extra SessionRegistry coverage: status counters, error state, injected broker.

Complements ``test_session.py`` (create/deploy/start/stop, cancellable loop,
idempotent stop, error paths) with the operational-snapshot details the
``get_session_status`` tool surfaces: bars_processed / orders_submitted /
trades_closed / open_position counts, the ``error`` state when the run loop raises,
and that a server-injected broker is honored by ``deploy`` (the testnet seam).

Deterministic + offline: finite in-memory feeds, no wall-clock, no network.
"""

from __future__ import annotations

import math

import pytest

from trader_mcp.execution import (
    ExecutionConfig,
    PaperBroker,
    SessionRegistry,
)
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
)

from .conftest import ListFeed, make_bars


def _spec() -> StrategySpec:
    return StrategySpec(
        name="ma-cross",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 3}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 8}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=50),
    )


def _signal_bars() -> list:
    """A sine path that produces multiple closed round-trips for a 3/8 MA cross."""
    return make_bars([100 + 10 * math.sin(i / 5.0) for i in range(160)])


@pytest.mark.asyncio
async def test_status_counts_orders_and_trades_after_run() -> None:
    """After a finite run, the status snapshot reports orders/trades/bars correctly."""
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    reg.deploy(info.session_id, _spec(), config=ExecutionConfig(initial_cash=10_000))
    bars = _signal_bars()
    task = reg.start(info.session_id, ListFeed(bars))
    await task

    status = reg.status(info.session_id)
    broker = reg.broker_for(info.session_id)
    assert broker is not None
    assert status.state == "stopped"
    assert status.bars_processed == len(bars)
    assert status.orders_submitted == len(broker.order_history())
    assert status.trades_closed == len(broker.trade_history())
    # The strategy crosses repeatedly -> at least one round-trip closed.
    assert status.trades_closed >= 1
    assert status.orders_submitted >= status.trades_closed


@pytest.mark.asyncio
async def test_stop_finalizes_open_inventory_into_trade_count() -> None:
    """stop() force-closes any open position so a held trade is realized on stop."""
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    reg.deploy(info.session_id, _spec())
    # Warm-up flat, a dip (so fast falls below slow), then a sustained rise: the
    # up-cross fires a long entry that is NEVER exited (no down-cross), so the
    # position is still OPEN at end-of-feed and only realizes on the stop force-close.
    closes = [100.0] * 12 + [95.0] * 6 + [100.0 + 2.0 * i for i in range(30)]
    bars = make_bars(closes)
    task = reg.start(info.session_id, ListFeed(bars))
    await task  # finite feed completes; position is still open
    # The position is open at end-of-feed (asserted before the force-close).
    pre_broker = reg.broker_for(info.session_id)
    assert pre_broker is not None
    assert pre_broker.positions()
    status = await reg.stop(info.session_id)  # force-close on stop
    broker = reg.broker_for(info.session_id)
    assert broker is not None
    assert not broker.positions()  # flattened by finalize()
    assert status.trades_closed == len(broker.trade_history())
    assert status.trades_closed >= 1


@pytest.mark.asyncio
async def test_run_loop_error_sets_error_state() -> None:
    """A feed that raises mid-stream lands the session in the 'error' state with a message."""
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    reg.deploy(info.session_id, _spec())

    class _BoomFeed:
        async def __aiter__(self):
            for bar in make_bars([100.0, 101.0, 102.0]):
                yield bar
            raise RuntimeError("feed exploded")

    task = reg.start(info.session_id, _BoomFeed())
    await task  # the runner swallows the exception and records it
    status = reg.status(info.session_id)
    assert status.state == "error"
    assert status.error is not None
    assert "exploded" in status.error
    # Bars processed before the explosion are still counted.
    assert status.bars_processed == 3


@pytest.mark.asyncio
async def test_stop_preserves_error_state() -> None:
    """Stopping an errored session does not overwrite its 'error' state."""
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    reg.deploy(info.session_id, _spec())

    class _BoomFeed:
        async def __aiter__(self):
            yield make_bars([100.0])[0]
            raise RuntimeError("kaboom")

    await reg.start(info.session_id, _BoomFeed())
    status = await reg.stop(info.session_id)
    assert status.state == "error"
    assert status.error is not None
    assert "kaboom" in status.error


def test_deploy_honors_injected_broker() -> None:
    """deploy() uses a caller-supplied broker (the server/testnet injection seam)."""
    reg = SessionRegistry()
    info = reg.create(mode="testnet", exchange="coinbase", symbol="BTC/USD")
    spec = _spec()
    injected = PaperBroker(spec, ExecutionConfig(initial_cash=1_234.0))
    reg.deploy(info.session_id, spec, broker=injected)
    broker = reg.broker_for(info.session_id)
    assert broker is not None
    assert broker is injected
    assert broker.balance().cash == 1_234.0


def test_status_of_undeployed_session_is_zeroed() -> None:
    """A created-but-undeployed session reports zero counters and no strategy."""
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    status = reg.status(info.session_id)
    assert status.strategy_name is None
    assert status.bars_processed == 0
    assert status.orders_submitted == 0
    assert status.trades_closed == 0
    assert status.open_position is False
    assert status.state == "stopped"


@pytest.mark.asyncio
async def test_double_start_raises_while_running() -> None:
    """Starting an already-running session raises (no concurrent run loops)."""
    import asyncio

    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    reg.deploy(info.session_id, _spec())

    async def _infinite():
        from datetime import UTC, datetime, timedelta

        from trader_mcp.exchanges.models import OHLCVBar

        t0 = datetime(2024, 1, 1, tzinfo=UTC)
        i = 0
        while True:
            yield OHLCVBar(
                timestamp=t0 + timedelta(hours=i),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1.0,
            )
            i += 1
            await asyncio.sleep(0)

    class _Feed:
        def __aiter__(self):
            return _infinite()

    reg.start(info.session_id, _Feed())
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError, match="already running"):
        reg.start(info.session_id, _Feed())
    await reg.stop(info.session_id)
