"""Tests for the Phase 3 strategy MCP resources.

Two resources expose saved strategy specs:

    * ``strategy://catalog`` -- the saved-strategy catalog as
      :class:`~trader_mcp.server.schemas.StrategyCatalogResult` JSON (each entry
      carrying its resolvable per-strategy URI);
    * ``strategy://{name}`` -- one saved strategy's full ``StrategySpec`` JSON,
      with the slugified name resolved back to the canonical name.

Fully offline: the strategy store is rooted at a tmp ``data_dir`` via
``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()``. Saving goes through the
``create_strategy`` tool so the round trip mirrors real client usage.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trader_mcp.config import get_settings
from trader_mcp.server.app import build_app
from trader_mcp.server.resources import (
    STRATEGY_CATALOG_URI,
    STRATEGY_URI_TEMPLATE,
)
from trader_mcp.server.schemas import StrategyCatalogResult
from trader_mcp.strategy import StrategySpec

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"


def _read_text(contents: Any) -> str:
    """Extract the single text payload from a FastMCP ``read_resource`` result."""
    items = list(contents)
    assert len(items) == 1
    payload = items[0].content
    assert isinstance(payload, str)
    return payload


def _valid_spec(name: str = "ma-cross-test") -> dict[str, Any]:
    return {
        "name": name,
        "exchange": _EXCHANGE,
        "symbol": _SYMBOL,
        "timeframe": _TIMEFRAME,
        "indicators": [
            {"id": "fast", "kind": "ema", "params": {"length": 20}},
            {"id": "slow", "kind": "ema", "params": {"length": 50}},
        ],
        "entry": {"long": "crossover(fast, slow)"},
        "exit": {"long": "crossunder(fast, slow)"},
        "position_sizing": {"mode": "percent_equity", "value": 10.0},
    }


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def app(data_dir: Path) -> Any:
    return build_app()


async def _save(app: Any, spec: dict[str, Any]) -> None:
    await app.call_tool("create_strategy", {"spec": spec})


# --------------------------------------------------------------------------- #
# Resource advertisement
# --------------------------------------------------------------------------- #
async def test_strategy_catalog_resource_is_listed() -> None:
    built = build_app()
    uris = {str(r.uri) for r in await built.list_resources()}
    assert STRATEGY_CATALOG_URI in uris


async def test_strategy_template_is_listed() -> None:
    built = build_app()
    templates = {t.uriTemplate for t in await built.list_resource_templates()}
    assert STRATEGY_URI_TEMPLATE in templates


# --------------------------------------------------------------------------- #
# Catalog resource
# --------------------------------------------------------------------------- #
async def test_strategy_catalog_empty(app: Any) -> None:
    payload = _read_text(await app.read_resource(STRATEGY_CATALOG_URI))
    result = StrategyCatalogResult.model_validate_json(payload)
    assert result.count == 0
    assert result.strategies == []


async def test_strategy_catalog_lists_saved_strategy(app: Any) -> None:
    await _save(app, _valid_spec())
    payload = _read_text(await app.read_resource(STRATEGY_CATALOG_URI))
    result = StrategyCatalogResult.model_validate_json(payload)
    assert result.count == 1
    entry = result.strategies[0]
    assert entry.name == "ma-cross-test"
    assert entry.resource_uri == "strategy://ma-cross-test"


# --------------------------------------------------------------------------- #
# Per-strategy resource round-trips the saved spec
# --------------------------------------------------------------------------- #
async def test_strategy_resource_round_trips_spec(app: Any) -> None:
    await _save(app, _valid_spec())
    payload = _read_text(await app.read_resource("strategy://ma-cross-test"))
    spec = StrategySpec.model_validate_json(payload)
    assert spec.name == "ma-cross-test"
    assert spec.symbol == _SYMBOL
    assert spec.indicators[0].params["length"] == 20.0
    assert spec.entry.long == "crossover(fast, slow)"


async def test_strategy_resource_not_found_raises(app: Any) -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017 - error type lives behind the SDK
        await app.read_resource("strategy://does-not-exist")
