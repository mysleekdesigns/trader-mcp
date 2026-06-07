"""Tests for the Phase 6 guardrails-only safety MCP tools (PRD §5.2 "safety").

Fully offline & deterministic. We seed a tmp ``data_dir`` with one synthetic
dataset + one saved strategy (the Phase 2/3/4/5 pattern), build the app, and drive
the tools through the FastMCP registry.

The load-bearing assertions are the safety guardrails:
  * ``set_risk_limits`` persists and is reflected by ``get_safety_status``;
  * an order breaching ``max_order_notional`` is DENIED with a redacted risk reason;
  * ``kill_switch engage`` halts new ``place_order`` (denied) and cancels open paper
    orders, and ``reset`` re-enables placement;
  * ``arm_live_trading`` is an expiring, confirmation-gated opt-in (and the wrong
    phrase fails closed) -- but no live order path consumes it (guardrails-only);
  * the redacted audit trail records intents/results/denials and carries no secret;
  * a normal paper ``place_order`` still simulates an OrderRecord (regression).
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
from trader_mcp.execution import OrderRecord, SessionInfo
from trader_mcp.safety import (
    MAX_ARM_TTL_SECONDS,
    REQUIRED_CONFIRMATION,
    ArmTicket,
    RiskLimits,
    SafetyStatus,
)
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
from trader_mcp.strategy import RiskLimits as StrategyRiskLimits

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"
_STRATEGY = "sma-cross-safetytest"
_N_BARS = 120
_SEED = 99

SAFETY_TOOLS = (
    "set_risk_limits",
    "arm_live_trading",
    "disarm_live_trading",
    "kill_switch",
    "get_safety_status",
    "get_audit_log",
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
        risk=StrategyRiskLimits(stop_loss_pct=3.0, take_profit_pct=6.0),
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


async def _start_paper(app: Any) -> SessionInfo:
    return SessionInfo.model_validate(
        _structured(
            await app.call_tool(
                "start_session",
                {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL},
            )
        )
    )


async def _place(app: Any, session_id: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "session_id": session_id,
        "symbol": _SYMBOL,
        "side": "buy",
        "amount": 0.01,
    }
    args.update(overrides)
    return _structured(await app.call_tool("place_order", args))


# --------------------------------------------------------------------------- #
# Registration + schema advertisement + tool count
# --------------------------------------------------------------------------- #
async def test_safety_tools_registered_and_structured() -> None:
    by_name = {t.name: t for t in await build_app().list_tools()}
    for name in SAFETY_TOOLS:
        assert name in by_name, f"{name} must be registered"
        tool = by_name[name]
        assert tool.outputSchema is not None, f"{name} must advertise an outputSchema"
        assert tool.outputSchema.get("type") == "object"


async def test_tool_count_is_45() -> None:
    assert len(await build_app().list_tools()) == 45


# --------------------------------------------------------------------------- #
# set_risk_limits persists + is reflected by get_safety_status
# --------------------------------------------------------------------------- #
async def test_set_risk_limits_persists_and_status_reflects(seeded_app: Any) -> None:
    stored = RiskLimits.model_validate(
        _structured(
            await seeded_app.call_tool(
                "set_risk_limits",
                {"max_order_notional": 1000.0, "max_open_positions": 2},
            )
        )
    )
    assert stored.max_order_notional == 1000.0
    assert stored.max_open_positions == 2

    status = SafetyStatus.model_validate(
        _structured(await seeded_app.call_tool("get_safety_status", {}))
    )
    assert status.risk_limits.max_order_notional == 1000.0
    assert status.risk_limits.max_open_positions == 2


# --------------------------------------------------------------------------- #
# arm_live_trading: correct phrase arms (expiring); wrong phrase fails closed;
# ttl is clamped; disarm clears it -- but NO live path consumes the arm.
# --------------------------------------------------------------------------- #
async def test_arm_live_trading_arms_and_disarm_clears(seeded_app: Any) -> None:
    ticket = ArmTicket.model_validate(
        _structured(
            await seeded_app.call_tool(
                "arm_live_trading",
                {"exchange": _EXCHANGE, "confirm": REQUIRED_CONFIRMATION, "ttl_seconds": 300},
            )
        )
    )
    assert ticket.exchange == _EXCHANGE
    assert ticket.armed_until > ticket.armed_at

    status = SafetyStatus.model_validate(
        _structured(await seeded_app.call_tool("get_safety_status", {}))
    )
    assert any(t.exchange == _EXCHANGE for t in status.armed)

    # Disarm clears it.
    await seeded_app.call_tool("disarm_live_trading", {"exchange": _EXCHANGE})
    status2 = SafetyStatus.model_validate(
        _structured(await seeded_app.call_tool("get_safety_status", {}))
    )
    assert not any(t.exchange == _EXCHANGE for t in status2.armed)


async def test_arm_live_trading_wrong_phrase_fails_closed(seeded_app: Any) -> None:
    with pytest.raises(Exception):  # noqa: PT011,B017 - SafetyError lives behind the SDK
        await seeded_app.call_tool(
            "arm_live_trading",
            {"exchange": _EXCHANGE, "confirm": "let me trade", "ttl_seconds": 300},
        )


async def test_arm_ttl_is_clamped_to_max(seeded_app: Any) -> None:
    ticket = ArmTicket.model_validate(
        _structured(
            await seeded_app.call_tool(
                "arm_live_trading",
                {
                    "exchange": "*",
                    "confirm": REQUIRED_CONFIRMATION,
                    "ttl_seconds": MAX_ARM_TTL_SECONDS * 10,
                },
            )
        )
    )
    ttl = (ticket.armed_until - ticket.armed_at).total_seconds()
    assert ttl <= MAX_ARM_TTL_SECONDS + 1


# --------------------------------------------------------------------------- #
# kill_switch: engage halts new place_order (denied) + cancels open paper orders;
# reset re-enables placement.
# --------------------------------------------------------------------------- #
async def test_kill_switch_blocks_new_orders_and_cancels_open(seeded_app: Any) -> None:
    info = await _start_paper(seeded_app)

    # A manual paper market order with no price stays OPEN (cancellable).
    first = OrderRecord.model_validate(await _place(seeded_app, info.session_id))
    assert first.status == "open"

    # Engage the kill switch for this exchange -> cancels the open paper order.
    ks = _structured(
        await seeded_app.call_tool(
            "kill_switch", {"action": "engage", "exchange": _EXCHANGE, "reason": "test halt"}
        )
    )
    assert ks["engaged"] is True
    assert ks["orders_canceled"] >= 1

    # A NEW order on the halted scope is denied (redacted SafetyError behind the SDK).
    with pytest.raises(Exception):  # noqa: PT011,B017
        await _place(seeded_app, info.session_id, seq=5)

    # Reset re-enables placement.
    reset = _structured(
        await seeded_app.call_tool("kill_switch", {"action": "reset", "exchange": _EXCHANGE})
    )
    assert reset["engaged"] is False
    after = OrderRecord.model_validate(await _place(seeded_app, info.session_id, seq=6))
    assert after.simulated is True


async def test_global_kill_switch_blocks_then_resets(seeded_app: Any) -> None:
    info = await _start_paper(seeded_app)
    await seeded_app.call_tool("kill_switch", {"action": "engage"})  # global
    with pytest.raises(Exception):  # noqa: PT011,B017
        await _place(seeded_app, info.session_id)
    await seeded_app.call_tool("kill_switch", {"action": "reset"})  # global reset
    rec = OrderRecord.model_validate(await _place(seeded_app, info.session_id, seq=9))
    assert rec.simulated is True


# --------------------------------------------------------------------------- #
# Risk limit enforcement at place_order: an over-notional order is denied.
# --------------------------------------------------------------------------- #
async def test_place_order_exceeding_max_notional_is_denied(seeded_app: Any) -> None:
    await seeded_app.call_tool("set_risk_limits", {"max_order_notional": 50.0})
    info = await _start_paper(seeded_app)
    # notional = amount * price = 1.0 * 100.0 = 100.0 > 50.0 -> denied on the risk rung.
    with pytest.raises(Exception):  # noqa: PT011,B017
        await _place(
            seeded_app,
            info.session_id,
            amount=1.0,
            type="limit",
            price=100.0,
        )

    # The denial is recorded in the audit log on the ``denied`` event.
    log = _structured(await seeded_app.call_tool("get_audit_log", {"event": "denied"}))
    assert log["count"] >= 1
    assert any("risk limit" in (e.get("detail") or "") for e in log["entries"])


async def test_market_order_no_price_with_notional_cap_fails_closed(seeded_app: Any) -> None:
    """A market order with no derivable price + a notional cap set is DENIED (fail-closed).

    The bug this guards against: notional = amount * 0.0 = 0.0 would make
    ``max_order_notional`` un-trippable for a market order, silently bypassing the cap.
    A fresh/flat paper session has no position mark to derive a price from, so the
    server must refuse rather than evaluate the cap against a meaningless zero.
    """
    await seeded_app.call_tool("set_risk_limits", {"max_order_notional": 50.0})
    info = await _start_paper(seeded_app)
    # Market order (no price), flat session -> no derivable ref price -> fail closed.
    with pytest.raises(Exception):  # noqa: PT011,B017 - SafetyError behind the SDK
        await _place(seeded_app, info.session_id, amount=1.0)

    log = _structured(await seeded_app.call_tool("get_audit_log", {"event": "denied"}))
    assert log["count"] >= 1
    assert any("without a known price" in (e.get("detail") or "") for e in log["entries"])


async def test_market_order_no_price_without_notional_cap_still_simulates(
    seeded_app: Any,
) -> None:
    """Regression: with NO notional cap set, a market order with no price still simulates.

    The fail-closed branch only triggers when a notional cap is configured; otherwise the
    existing zero-notional market-order behaviour is unchanged.
    """
    info = await _start_paper(seeded_app)
    record = OrderRecord.model_validate(await _place(seeded_app, info.session_id, amount=1.0))
    assert record.simulated is True
    assert record.client_order_id.startswith("tmcp-")


# --------------------------------------------------------------------------- #
# Audit log: intents/results/denials recorded, redacted, no secret material.
# --------------------------------------------------------------------------- #
async def test_audit_log_records_intent_and_result(seeded_app: Any) -> None:
    info = await _start_paper(seeded_app)
    await _place(seeded_app, info.session_id)

    intents = _structured(await seeded_app.call_tool("get_audit_log", {"event": "order_intent"}))
    results = _structured(await seeded_app.call_tool("get_audit_log", {"event": "order_result"}))
    assert intents["count"] >= 1
    assert results["count"] >= 1

    # No secret material: details carry no key-like material. We seeded no real keys,
    # so assert the redaction sentinel is absent and the entries are well-formed.
    full = _structured(await seeded_app.call_tool("get_audit_log", {}))
    for entry in full["entries"]:
        detail = entry.get("detail") or ""
        # A paper session has no credentials, so nothing should ever be redacted here;
        # and no entry should leak an env/key token.
        assert "secret" not in detail.lower()
        assert "api_key" not in detail.lower()
    assert full["count"] == len(full["entries"])


async def test_audit_log_limit_paginates(seeded_app: Any) -> None:
    info = await _start_paper(seeded_app)
    for i in range(3):
        await _place(seeded_app, info.session_id, seq=100 + i)
    one = _structured(await seeded_app.call_tool("get_audit_log", {"limit": 1}))
    assert one["count"] == 1


# --------------------------------------------------------------------------- #
# Regression: a normal paper place_order still simulates an OrderRecord.
# --------------------------------------------------------------------------- #
async def test_normal_paper_place_order_still_simulates(seeded_app: Any) -> None:
    info = await _start_paper(seeded_app)
    record = OrderRecord.model_validate(await _place(seeded_app, info.session_id))
    assert record.simulated is True
    assert record.client_order_id.startswith("tmcp-")


# --------------------------------------------------------------------------- #
# disarm_live_trading returns a typed result.
# --------------------------------------------------------------------------- #
async def test_disarm_returns_typed_scope(seeded_app: Any) -> None:
    out = _structured(await seeded_app.call_tool("disarm_live_trading", {}))
    assert out["disarmed"] == "*"
    out2 = _structured(await seeded_app.call_tool("disarm_live_trading", {"exchange": _EXCHANGE}))
    assert out2["disarmed"] == _EXCHANGE
