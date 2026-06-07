"""QA-complement tests for the Phase 5 execution MCP tools (PRD §5.2, §6).

Complements ``test_execution_tools.py`` (registration, paper place_order is
simulated, paper deploy runs the runtime, unknown-session raises) with the
remaining tool-surface coverage and the cross-cutting safe-by-default checks:

  * the read tools (get_open_orders / get_positions / get_balance) return typed,
    internally-consistent results, and every reported order is simulated;
  * cancel_order cancels a pending paper order;
  * NO arming / kill-switch / live-trading tool is exposed in Phase 5 (Phase-6 wall);
  * the registered tool count is exactly 39.

Fully offline & deterministic (tmp data_dir + seeded synthetic dataset/strategy;
the Phase 2/3/4 pattern). No exchange network is touched.
"""

from __future__ import annotations

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
from trader_mcp.execution import OrderRecord, PaperBalance, SessionInfo
from trader_mcp.server.app import build_app
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
    StrategyStore,
)

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"
_STRATEGY = "sma-cross-qatest"
_N_BARS = 200
_SEED = 9090


def _structured(result: Any) -> dict[str, Any]:
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def _seed_bars() -> list[OHLCVBar]:
    rng = np.random.default_rng(_SEED)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 8.0 * np.sin(t / 13.0) + rng.normal(0, 1, _N_BARS).cumsum() * 0.1, 1.0
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


def _spec() -> StrategySpec:
    return StrategySpec(
        name=_STRATEGY,
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 5}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 15}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=20.0),
        fees=Fees(),
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
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


async def _start_paper(app: Any) -> str:
    info = SessionInfo.model_validate(
        _structured(
            await app.call_tool(
                "start_session", {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL}
            )
        )
    )
    return info.session_id


# --------------------------------------------------------------------------- #
# Phase 6 guardrails-only: the safety tools exist, but the live WALL stays shut
# --------------------------------------------------------------------------- #
async def test_phase6_exposes_guardrails_but_no_live_order_path() -> None:
    """Guardrails-only: the safety surface lands, but no live-order tool exists.

    Phase 6 adds ``set_risk_limits`` / ``arm_live_trading`` / ``kill_switch`` (and
    their companions), but the live wall is untouched: there is NO ``place_live_order``
    / ``deploy_live`` tool, and ``arm_live_trading`` only records state no live path
    consumes yet (asserted by the safety-tools suite).
    """
    names = {t.name for t in await build_app().list_tools()}
    assert {"set_risk_limits", "arm_live_trading", "kill_switch"} <= names
    # The live order path remains unbuilt -- no tool routes a real-money order.
    assert names.isdisjoint({"place_live_order", "deploy_live", "place_real_order"})


async def test_tool_count_is_exactly_46() -> None:
    names = [t.name for t in await build_app().list_tools()]
    assert len(names) == 46, sorted(names)
    # No duplicate registrations.
    assert len(set(names)) == len(names)


# --------------------------------------------------------------------------- #
# Read tools: typed + consistent + simulated
# --------------------------------------------------------------------------- #
async def test_read_tools_typed_and_simulated(seeded_app: Any) -> None:
    sid = await _start_paper(seeded_app)
    # Place a pending paper order so open_orders is non-empty.
    record = OrderRecord.model_validate(
        _structured(
            await seeded_app.call_tool(
                "place_order",
                {"session_id": sid, "symbol": _SYMBOL, "side": "buy", "amount": 0.01},
            )
        )
    )
    assert record.simulated is True

    open_orders = _structured(await seeded_app.call_tool("get_open_orders", {"session_id": sid}))
    assert open_orders["session_id"] == sid
    assert open_orders["count"] == len(open_orders["orders"]) >= 1
    assert all(o["simulated"] is True for o in open_orders["orders"])

    positions = _structured(await seeded_app.call_tool("get_positions", {"session_id": sid}))
    assert positions["count"] == len(positions["positions"])

    balance = PaperBalance.model_validate(
        _structured(await seeded_app.call_tool("get_balance", {"session_id": sid}))
    )
    assert balance.cash >= 0.0


async def test_cancel_order_cancels_pending_paper_order(seeded_app: Any) -> None:
    """A manual paper order on an undeployed session can be canceled (regression guard).

    Once a bug: cancel_order's paper branch read ``session_registry.broker_for``
    directly and missed the manual-broker fallback (``_read_paper_broker``) the other
    read tools use, so a manual order placed on an undeployed session was visible via
    get_open_orders but could not be canceled. Fixed in src/; now asserted hard.
    """
    sid = await _start_paper(seeded_app)
    record = OrderRecord.model_validate(
        _structured(
            await seeded_app.call_tool(
                "place_order",
                {"session_id": sid, "symbol": _SYMBOL, "side": "buy", "amount": 0.01},
            )
        )
    )
    # The order is visible via get_open_orders (proving it was recorded)...
    open_before = _structured(await seeded_app.call_tool("get_open_orders", {"session_id": sid}))
    assert any(o["order_id"] == record.order_id for o in open_before["orders"])

    cancel = _structured(
        await seeded_app.call_tool("cancel_order", {"session_id": sid, "order_id": record.order_id})
    )
    assert cancel["session_id"] == sid
    assert cancel["order_id"] == record.order_id
    # ...and cancel_order now finds the manual broker via the fallback.
    assert cancel["canceled"] is True

    # After cancel, the order is no longer open.
    open_orders = _structured(await seeded_app.call_tool("get_open_orders", {"session_id": sid}))
    assert all(o["order_id"] != record.order_id for o in open_orders["orders"])


async def test_place_order_client_order_id_is_deterministic_prefix(seeded_app: Any) -> None:
    """Every routed/simulated order carries a deterministic tmcp- client order id."""
    sid = await _start_paper(seeded_app)
    record = OrderRecord.model_validate(
        _structured(
            await seeded_app.call_tool(
                "place_order",
                {"session_id": sid, "symbol": _SYMBOL, "side": "buy", "amount": 0.05},
            )
        )
    )
    assert record.client_order_id.startswith("tmcp-")
    assert record.simulated is True


async def test_deploy_unknown_strategy_raises(seeded_app: Any) -> None:
    sid = await _start_paper(seeded_app)
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await seeded_app.call_tool(
            "deploy_strategy", {"session_id": sid, "strategy_name": "no-such-strategy"}
        )
    msg = str(excinfo.value).lower()
    assert "no-such-strategy" in msg or "not found" in msg or "unknown" in msg
