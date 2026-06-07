"""Smoke tests for the Phase 5 paper/testnet execution + portfolio MCP tools.

Fully offline & deterministic. We seed a tmp ``data_dir`` with one synthetic
dataset + one saved strategy (the Phase 2/3/4 pattern), build the app, and drive
the tools through the FastMCP registry.

The load-bearing assertions are the safe-by-default invariant: a ``paper``
``place_order`` is SIMULATED (``simulated=True``, never routed), and a ``paper``
``deploy_strategy`` runs the SAME interpreter/runtime over the cached replay feed
with no network. (Routed testnet behaviour is exercised by the execution package's
own suite + QA's fakes; here we only assert paper safety and the wiring.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trader_mcp.config import get_settings
from trader_mcp.data.models import DatasetKey
from trader_mcp.data.store import OHLCVStore
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.execution import OrderRecord, SessionInfo, SessionStatus
from trader_mcp.server.app import build_app
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
    StrategyStore,
)

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"
_STRATEGY = "sma-cross-exectest"
_N_BARS = 300
_SEED = 7

PHASE5_TOOLS = (
    "start_session",
    "place_order",
    "cancel_order",
    "get_open_orders",
    "get_positions",
    "get_balance",
    "deploy_strategy",
    "stop_strategy",
    "get_session_status",
    "get_portfolio",
    "get_pnl",
    "get_trade_history",
)


def _structured(result: Any) -> dict[str, Any]:
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def _seed_bars() -> list[OHLCVBar]:
    rng = np.random.default_rng(_SEED)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 8.0 * np.sin(t / 15.0) + rng.normal(0, 1, _N_BARS).cumsum() * 0.1, 1.0
    )
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        close = float(c)
        open_px = prev if i > 0 else close
        bars.append(
            OHLCVBar(
                timestamp=t0 + timedelta(hours=i),
                open=open_px,
                high=max(open_px, close) * 1.002,
                low=min(open_px, close) * 0.998,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return bars


def _spec(name: str = _STRATEGY) -> StrategySpec:
    return StrategySpec(
        name=name,
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 10}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 30}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=20.0),
        risk=RiskLimits(stop_loss_pct=3.0, take_profit_pct=6.0),
        fees=Fees(),
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    # Force the safe-by-default dry-run ON regardless of the developer's environment,
    # so the routing-downgrade behaviour is deterministic in tests.
    monkeypatch.delenv("TRADER_MCP_DRY_RUN", raising=False)
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def seeded_app(data_dir: Path) -> Any:
    OHLCVStore(data_dir).upsert_bars(
        DatasetKey(exchange=_EXCHANGE, symbol=_SYMBOL, timeframe=_TIMEFRAME), _seed_bars()
    )
    StrategyStore(data_dir).save(_spec())
    return build_app()


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
async def test_phase5_tools_registered() -> None:
    names = {t.name for t in await build_app().list_tools()}
    assert set(PHASE5_TOOLS) <= names


async def test_phase5_tools_advertise_structured_output_schema() -> None:
    by_name = {t.name: t for t in await build_app().list_tools()}
    for name in PHASE5_TOOLS:
        tool = by_name[name]
        assert tool.outputSchema is not None, f"{name} must advertise an outputSchema"
        assert tool.outputSchema.get("type") == "object"


async def test_tool_count_is_46() -> None:
    assert len(await build_app().list_tools()) == 46


# --------------------------------------------------------------------------- #
# Safe-by-default: a paper place_order is SIMULATED, never routed
# --------------------------------------------------------------------------- #
async def test_paper_place_order_is_simulated(seeded_app: Any) -> None:
    info = SessionInfo.model_validate(
        _structured(
            await seeded_app.call_tool(
                "start_session",
                {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL},
            )
        )
    )
    assert info.mode == "paper"

    record = OrderRecord.model_validate(
        _structured(
            await seeded_app.call_tool(
                "place_order",
                {
                    "session_id": info.session_id,
                    "symbol": _SYMBOL,
                    "side": "buy",
                    "amount": 0.01,
                },
            )
        )
    )
    # The load-bearing safety assertion: a paper order never routes to a real venue.
    assert record.simulated is True
    assert record.client_order_id.startswith("tmcp-")


# --------------------------------------------------------------------------- #
# Paper deploy runs the runtime over the cached replay feed (no network)
# --------------------------------------------------------------------------- #
async def test_paper_deploy_runs_runtime_over_cached_bars(seeded_app: Any) -> None:
    info = SessionInfo.model_validate(
        _structured(
            await seeded_app.call_tool(
                "start_session",
                {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL},
            )
        )
    )
    deployed = SessionInfo.model_validate(
        _structured(
            await seeded_app.call_tool(
                "deploy_strategy",
                {"session_id": info.session_id, "strategy_name": _STRATEGY},
            )
        )
    )
    assert deployed.strategy_name == _STRATEGY

    # Let the replay run loop drain the finite cached feed (one bar per loop turn),
    # then stop. We yield generously past the bar count so the finite feed exhausts.
    for _ in range(_N_BARS + 50):
        await asyncio.sleep(0)
    status = SessionStatus.model_validate(
        _structured(await seeded_app.call_tool("stop_strategy", {"session_id": info.session_id}))
    )
    # The same interpreter/runtime consumed the cached bars (parity path), offline.
    assert status.bars_processed == _N_BARS
    assert status.state in ("stopped", "error")
    assert status.error is None


async def test_get_session_status_unknown_id_raises(seeded_app: Any) -> None:
    with pytest.raises(Exception):  # noqa: PT011,B017 - error type lives behind the SDK
        await seeded_app.call_tool("get_session_status", {"session_id": "nope"})


# --------------------------------------------------------------------------- #
# Idempotent, distinct client order IDs (PRD §6 Phase 5 reconciliation item)
# --------------------------------------------------------------------------- #
async def _start_paper(app: Any) -> SessionInfo:
    return SessionInfo.model_validate(
        _structured(
            await app.call_tool(
                "start_session",
                {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL},
            )
        )
    )


async def test_distinct_orders_get_distinct_client_order_ids(seeded_app: Any) -> None:
    """Two orders on the same (session, symbol, side) get DISTINCT, deterministic COIDs."""
    info = await _start_paper(seeded_app)
    args = {"session_id": info.session_id, "symbol": _SYMBOL, "side": "buy", "amount": 0.01}
    first = OrderRecord.model_validate(_structured(await seeded_app.call_tool("place_order", args)))
    second = OrderRecord.model_validate(
        _structured(await seeded_app.call_tool("place_order", args))
    )
    # Auto-incremented per-session seq -> distinct COIDs -> two real orders recorded.
    assert first.client_order_id != second.client_order_id
    assert first.client_order_id.startswith("tmcp-")
    assert second.client_order_id.startswith("tmcp-")

    listed = _structured(
        await seeded_app.call_tool("get_open_orders", {"session_id": info.session_id})
    )
    assert listed["count"] == 2


async def test_duplicate_coid_is_deduped(seeded_app: Any) -> None:
    """A resubmission with the SAME explicit seq is deduped: same COID, no 2nd order."""
    info = await _start_paper(seeded_app)
    args = {
        "session_id": info.session_id,
        "symbol": _SYMBOL,
        "side": "buy",
        "amount": 0.01,
        "seq": 42,
    }
    first = OrderRecord.model_validate(_structured(await seeded_app.call_tool("place_order", args)))
    # Retry with the identical seq -> same deterministic COID -> deduped to the prior.
    retry = OrderRecord.model_validate(_structured(await seeded_app.call_tool("place_order", args)))
    assert retry.client_order_id == first.client_order_id
    assert retry.order_id == first.order_id

    # The dedupe means only ONE order was actually recorded on the broker.
    listed = _structured(
        await seeded_app.call_tool("get_open_orders", {"session_id": info.session_id})
    )
    assert listed["count"] == 1
