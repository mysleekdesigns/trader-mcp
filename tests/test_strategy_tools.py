"""Tests for the Phase 3 MCP strategy-authoring tool surface.

These tools wrap the declarative-strategy public API (templates, validation,
create/get/list/update/delete) and return typed Pydantic v2 models, so every
structured tool output must validate against its declared model and every tool
must advertise an ``outputSchema``.

Fully offline: there is no network or execution path in Phase 3 -- strategy
authoring is pure CRUD over the local file store, rooted at a tmp ``data_dir`` via
``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()`` (the same env/cache
pattern the Phase 2 data tools use). The end-to-end path asserted here is the
create -> get -> list -> update -> delete round trip through the REAL file store,
plus template/indicator listing and the AI-friendly validate path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trader_mcp.config import get_settings
from trader_mcp.server.app import build_app
from trader_mcp.server.schemas import (
    CreateStrategyResult,
    DeleteResult,
    IndicatorsResult,
    StrategiesResult,
    TemplatesResult,
)
from trader_mcp.strategy import StrategySpec, ValidationReport

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-output dict from a FastMCP ``call_tool`` result."""
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def _valid_spec(name: str = "ma-cross-test", *, fast: int = 20, slow: int = 50) -> dict[str, Any]:
    """A minimal, valid rule-strategy spec payload (EMA crossover)."""
    return {
        "name": name,
        "exchange": _EXCHANGE,
        "symbol": _SYMBOL,
        "timeframe": _TIMEFRAME,
        "indicators": [
            {"id": "fast", "kind": "ema", "params": {"length": fast}},
            {"id": "slow", "kind": "ema", "params": {"length": slow}},
        ],
        "entry": {"long": "crossover(fast, slow)"},
        "exit": {"long": "crossunder(fast, slow)"},
        "position_sizing": {"mode": "percent_equity", "value": 10.0},
    }


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Root the strategy store at a tmp dir for the duration of a test."""
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def app(data_dir: Path) -> Any:
    """A built app whose strategy store is rooted at the tmp ``data_dir``."""
    return build_app()


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
PHASE3_TOOLS = (
    "list_strategy_templates",
    "list_indicators",
    "validate_strategy",
    "create_strategy",
    "get_strategy",
    "list_strategies",
    "update_strategy",
    "delete_strategy",
)


async def test_phase3_tools_registered() -> None:
    built = build_app()
    names = {t.name for t in await built.list_tools()}
    assert set(PHASE3_TOOLS) <= names


async def test_phase3_tools_advertise_structured_output_schema() -> None:
    built = build_app()
    by_name = {t.name: t for t in await built.list_tools()}
    for name in PHASE3_TOOLS:
        tool = by_name[name]
        assert tool.outputSchema is not None, f"{name} must advertise an outputSchema"
        assert tool.outputSchema.get("type") == "object"


async def test_no_live_order_path_introduced() -> None:
    """The live WALL stays shut even after the Phase-6 guardrails land.

    Phase 6 adds the guardrails-only safety surface (``arm_live_trading`` /
    ``kill_switch`` / ``set_risk_limits``), but those configure/observe state only --
    no real-money order path exists: there is no ``place_live_order`` / ``deploy_live``
    tool, and ``arm_live_trading`` gates nothing yet (deferred to live certification).
    """
    built = build_app()
    names = {t.name for t in await built.list_tools()}
    assert names.isdisjoint({"place_live_order", "deploy_live", "place_real_order"})


# --------------------------------------------------------------------------- #
# Templates & indicators
# --------------------------------------------------------------------------- #
async def test_list_strategy_templates(app: Any) -> None:
    result = TemplatesResult.model_validate(
        _structured(await app.call_tool("list_strategy_templates", {}))
    )
    assert result.count == len(result.templates)
    ids = {t.id for t in result.templates}
    assert {"ma_cross", "rsi_reversion", "grid", "dca"} <= ids


async def test_list_indicators(app: Any) -> None:
    result = IndicatorsResult.model_validate(
        _structured(await app.call_tool("list_indicators", {}))
    )
    assert result.count == len(result.indicators) > 0
    kinds = {i.kind for i in result.indicators}
    assert {"ema", "rsi", "macd"} <= kinds


# --------------------------------------------------------------------------- #
# Validation (AI-friendly, never raises)
# --------------------------------------------------------------------------- #
async def test_validate_strategy_ok(app: Any) -> None:
    report = ValidationReport.model_validate(
        _structured(await app.call_tool("validate_strategy", {"spec": _valid_spec()}))
    )
    assert report.ok is True
    assert report.issues == []


async def test_validate_strategy_reports_issues_for_bad_input(app: Any) -> None:
    bad = _valid_spec()
    bad["timeframe"] = "3m"  # not a supported timeframe
    report = ValidationReport.model_validate(
        _structured(await app.call_tool("validate_strategy", {"spec": bad}))
    )
    assert report.ok is False
    assert report.issues  # at least one actionable issue
    assert all(i.location and i.problem and i.fix for i in report.issues)


async def test_validate_strategy_rejects_non_whitelisted_indicator(app: Any) -> None:
    bad = _valid_spec()
    bad["indicators"] = [{"id": "x", "kind": "not_a_real_indicator"}]
    report = ValidationReport.model_validate(
        _structured(await app.call_tool("validate_strategy", {"spec": bad}))
    )
    assert report.ok is False


# --------------------------------------------------------------------------- #
# create_strategy: surfaces structured issues on bad input
# --------------------------------------------------------------------------- #
async def test_create_strategy_rejects_bad_input(app: Any) -> None:
    bad = _valid_spec()
    bad["timeframe"] = "3m"
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app.call_tool("create_strategy", {"spec": bad})
    # The structured invalid_strategy_spec details must reach the client.
    msg = str(excinfo.value).lower()
    assert "timeframe" in msg or "invalid" in msg


# --------------------------------------------------------------------------- #
# End-to-end: create -> get -> list -> update -> delete (the Phase 3 contract)
# --------------------------------------------------------------------------- #
async def test_create_get_list_update_delete_round_trip(app: Any) -> None:
    # create
    created = CreateStrategyResult.model_validate(
        _structured(await app.call_tool("create_strategy", {"spec": _valid_spec()}))
    )
    assert created.spec.name == "ma-cross-test"
    assert created.info.name == "ma-cross-test"
    assert created.info.created is not None
    assert created.info.updated is not None
    original_created = created.info.created

    # get
    fetched = StrategySpec.model_validate(
        _structured(await app.call_tool("get_strategy", {"name": "ma-cross-test"}))
    )
    assert fetched.name == "ma-cross-test"
    assert fetched.indicators[0].params["length"] == 20.0

    # list
    listed = StrategiesResult.model_validate(
        _structured(await app.call_tool("list_strategies", {}))
    )
    assert listed.count == 1
    assert listed.strategies[0].name == "ma-cross-test"

    # update (same name; change a param) -- preserves created, refreshes updated
    updated = CreateStrategyResult.model_validate(
        _structured(
            await app.call_tool(
                "update_strategy",
                {"name": "ma-cross-test", "spec": _valid_spec(fast=10)},
            )
        )
    )
    assert updated.spec.indicators[0].params["length"] == 10.0
    assert updated.info.created == original_created

    # delete
    deleted = DeleteResult.model_validate(
        _structured(await app.call_tool("delete_strategy", {"name": "ma-cross-test"}))
    )
    assert deleted.name == "ma-cross-test"
    assert deleted.deleted is True

    # list is empty again
    empty = StrategiesResult.model_validate(_structured(await app.call_tool("list_strategies", {})))
    assert empty.count == 0


async def test_get_strategy_not_found_raises(app: Any) -> None:
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app.call_tool("get_strategy", {"name": "does-not-exist"})
    assert (
        "does-not-exist" in str(excinfo.value) or "no saved strategy" in str(excinfo.value).lower()
    )


async def test_update_strategy_missing_raises(app: Any) -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017 - error type lives behind the SDK
        await app.call_tool(
            "update_strategy",
            {"name": "missing", "spec": _valid_spec(name="missing")},
        )


async def test_update_strategy_rejects_rename(app: Any) -> None:
    await app.call_tool("create_strategy", {"spec": _valid_spec(name="orig")})
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app.call_tool(
            "update_strategy",
            {"name": "orig", "spec": _valid_spec(name="renamed")},
        )
    assert "rename" in str(excinfo.value).lower() or "match" in str(excinfo.value).lower()


async def test_delete_missing_is_idempotent(app: Any) -> None:
    result = DeleteResult.model_validate(
        _structured(await app.call_tool("delete_strategy", {"name": "never-existed"}))
    )
    assert result.deleted is False
