"""Tests for the Phase 5 portfolio & analytics MCP tools (PRD §5.2, §6).

The three read-only tools -- ``get_portfolio`` / ``get_pnl`` / ``get_trade_history``
-- view a session's in-memory broker through the registry. They place no orders and
touch no network. We assert typed Pydantic v2 output and that, after a paper deploy
runs the cached replay feed, the portfolio/pnl/trade-history are internally
consistent (equity == cash + position_value; pnl.total == realized + unrealized;
the trade list count matches).

Fully offline & deterministic: a tmp ``data_dir`` seeded with one synthetic dataset
+ one saved strategy (the Phase 2/3/4 pattern), built into the app, driven through
the FastMCP registry. Synthetic bars come from a fixed numpy Generator.
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
from trader_mcp.execution import PnLBreakdown, Portfolio, SessionInfo
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
_STRATEGY = "sma-cross-porttest"
_N_BARS = 250
_SEED = 1357

PORTFOLIO_TOOLS = ("get_portfolio", "get_pnl", "get_trade_history")


def _structured(result: Any) -> dict[str, Any]:
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def _seed_bars() -> list[OHLCVBar]:
    rng = np.random.default_rng(_SEED)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 10.0 * np.sin(t / 11.0) + rng.normal(0, 1, _N_BARS).cumsum() * 0.1, 1.0
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
            IndicatorSpec(id="fast", kind="sma", params={"length": 5}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 15}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=25.0),
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


async def _start_and_run(app: Any) -> str:
    """Start a paper session, deploy, drain the finite cached feed, stop; return id."""
    info = SessionInfo.model_validate(
        _structured(
            await app.call_tool(
                "start_session", {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL}
            )
        )
    )
    await app.call_tool(
        "deploy_strategy", {"session_id": info.session_id, "strategy_name": _STRATEGY}
    )
    for _ in range(_N_BARS + 50):
        await asyncio.sleep(0)
    await app.call_tool("stop_strategy", {"session_id": info.session_id})
    return info.session_id


# --------------------------------------------------------------------------- #
# Registration + schema
# --------------------------------------------------------------------------- #
async def test_portfolio_tools_registered_with_schema() -> None:
    by_name = {t.name: t for t in await build_app().list_tools()}
    for name in PORTFOLIO_TOOLS:
        assert name in by_name, name
        assert by_name[name].outputSchema is not None
        assert by_name[name].outputSchema.get("type") == "object"


# --------------------------------------------------------------------------- #
# Typed results on a session with no broker yet (created but not deployed)
# --------------------------------------------------------------------------- #
async def test_portfolio_pnl_history_typed_on_fresh_session(seeded_app: Any) -> None:
    info = SessionInfo.model_validate(
        _structured(
            await seeded_app.call_tool(
                "start_session", {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL}
            )
        )
    )
    sid = info.session_id

    portfolio = Portfolio.model_validate(
        _structured(await seeded_app.call_tool("get_portfolio", {"session_id": sid}))
    )
    assert portfolio.position is None  # no trading yet

    pnl = PnLBreakdown.model_validate(
        _structured(await seeded_app.call_tool("get_pnl", {"session_id": sid}))
    )
    assert pnl.total == pnl.realized + pnl.unrealized

    history = _structured(await seeded_app.call_tool("get_trade_history", {"session_id": sid}))
    assert history["session_id"] == sid
    assert history["count"] == len(history["trades"]) == 0


# --------------------------------------------------------------------------- #
# After a deployed run, the views are internally consistent
# --------------------------------------------------------------------------- #
async def test_portfolio_views_consistent_after_run(seeded_app: Any) -> None:
    sid = await _start_and_run(seeded_app)

    portfolio = Portfolio.model_validate(
        _structured(await seeded_app.call_tool("get_portfolio", {"session_id": sid}))
    )
    # equity == cash + position_value (the broker's mark-to-market identity).
    assert abs(portfolio.equity - (portfolio.cash + portfolio.position_value)) < 1e-6

    pnl = PnLBreakdown.model_validate(
        _structured(await seeded_app.call_tool("get_pnl", {"session_id": sid}))
    )
    assert abs(pnl.total - (pnl.realized + pnl.unrealized)) < 1e-9
    assert pnl.fees_paid >= 0.0

    history = _structured(await seeded_app.call_tool("get_trade_history", {"session_id": sid}))
    assert history["count"] == len(history["trades"])
    # The SMA-cross over a 250-bar sine path closes at least one round-trip.
    assert history["count"] >= 1
    for trade in history["trades"]:
        assert trade["side"] in ("long", "short")
        assert trade["exit_reason"]  # non-empty reason


async def test_pnl_return_pct_tracks_equity_change(seeded_app: Any) -> None:
    """return_pct equals (equity - initial_cash) / initial_cash after a flat-on-stop run."""
    sid = await _start_and_run(seeded_app)
    portfolio = Portfolio.model_validate(
        _structured(await seeded_app.call_tool("get_portfolio", {"session_id": sid}))
    )
    pnl = PnLBreakdown.model_validate(
        _structured(await seeded_app.call_tool("get_pnl", {"session_id": sid}))
    )
    # After stop (force-close), the session is flat: equity == cash and total == realized.
    assert portfolio.position is None
    # return_pct is total over the initial cash (10k default ExecutionConfig).
    assert abs(pnl.return_pct - (pnl.total / 10_000.0 * 100.0)) < 1e-6


async def test_portfolio_unknown_session_raises(seeded_app: Any) -> None:
    with pytest.raises(Exception):  # noqa: PT011,B017 - error type lives behind the SDK
        await seeded_app.call_tool("get_portfolio", {"session_id": "nope"})
