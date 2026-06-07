"""SessionRegistry lifecycle: create / deploy / start / stop, cancellable run loop."""

from __future__ import annotations

import asyncio

import pytest

from trader_mcp.execution import ExecutionConfig, SessionRegistry
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


def test_create_and_list_isolated_registries() -> None:
    reg_a = SessionRegistry()
    reg_b = SessionRegistry()
    info = reg_a.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    assert info.session_id.startswith("sess-")
    assert info.strategy_name is None
    assert len(reg_a.list()) == 1
    assert reg_b.list() == []  # no shared global state


def test_deploy_binds_runtime_and_strategy_name() -> None:
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    spec = _spec()
    updated = reg.deploy(info.session_id, spec, config=ExecutionConfig(initial_cash=5_000))
    assert updated.strategy_name == "ma-cross"
    assert reg.runtime_for(info.session_id) is not None
    assert reg.broker_for(info.session_id) is not None


@pytest.mark.asyncio
async def test_run_loop_processes_bars_and_stops_cleanly() -> None:
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    spec = _spec()
    reg.deploy(info.session_id, spec)
    import math

    bars = make_bars([100 + 10 * math.sin(i / 5.0) for i in range(120)])
    feed = ListFeed(bars)
    task = reg.start(info.session_id, feed)
    await task  # finite feed -> completes on its own
    status = reg.status(info.session_id)
    assert status.state == "stopped"
    assert status.bars_processed == len(bars)
    assert status.last_bar_time == bars[-1].timestamp


@pytest.mark.asyncio
async def test_stop_cancels_a_running_loop() -> None:
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    spec = _spec()
    reg.deploy(info.session_id, spec)

    # An infinite feed so the loop keeps running until cancelled.
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
    await asyncio.sleep(0.01)  # let it process some bars
    status = await reg.stop(info.session_id)
    assert status.state == "stopped"
    assert status.bars_processed > 0


@pytest.mark.asyncio
async def test_stop_is_idempotent_on_idle_session() -> None:
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    reg.deploy(info.session_id, _spec())
    status = await reg.stop(info.session_id)  # never started
    assert status.state == "stopped"


def test_start_without_deploy_raises() -> None:
    reg = SessionRegistry()
    info = reg.create(mode="paper", exchange="coinbase", symbol="BTC/USD")
    with pytest.raises(RuntimeError):
        reg.start(info.session_id, ListFeed([]))


def test_unknown_session_raises() -> None:
    reg = SessionRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")
