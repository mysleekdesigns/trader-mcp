"""Tests for the Phase 2 cached-dataset MCP resources.

The catalog resource (``dataset://catalog``) and the per-dataset coverage template
(``dataset://{exchange}/{symbol}/{timeframe}``) are read-only views over the local
OHLCV cache. These tests sync a dataset into a tmp cache, then assert the resources
list and read back the expected JSON. The per-dataset template accepts the
SANITIZED symbol form in the URI (``BTC-USD``) and resolves it to the canonical
``BTC/USD`` via the catalog -- the catalog embeds each dataset's resolvable URI so a
client can discover the exact form.

Fully offline, mirroring ``tests/test_historical_tools.py``: the paginating fake is
injected behind the adapter's client seam and the cache is rooted at a tmp dir via
``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests.data._ohlcv_fakes import PaginatingFakeCcxt
from trader_mcp.config import get_settings
from trader_mcp.data import DatasetInspection
from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.server.app import build_app
from trader_mcp.server.resources import CATALOG_URI, DATASET_URI_TEMPLATE
from trader_mcp.server.schemas import CatalogResult

_TIMEFRAME = "1h"
_SYMBOL = "BTC/USD"
_SANITIZED = "BTC-USD"
_EXCHANGE = "coinbase"
_STEP_MS = timeframe_ms(_TIMEFRAME)
_BAR_COUNT = 400
_START = datetime(2023, 1, 1, tzinfo=UTC)
_START_MS = int(_START.timestamp() * 1000)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def app_with_data(
    monkeypatch: pytest.MonkeyPatch,
    no_credentials: None,
    data_dir: Path,
) -> Any:
    """A built app with one synced BTC/USD 1h dataset in a tmp cache."""
    fake = PaginatingFakeCcxt(
        timeframe=_TIMEFRAME,
        start_ms=_START_MS,
        bar_count=_BAR_COUNT,
        page_cap=300,
    )

    def factory(exchange_id: str, config: dict[str, object]) -> PaginatingFakeCcxt:
        return fake

    monkeypatch.setattr(adapter_module, "_create_ccxt_client", factory)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(adapter_module, "_sleep", no_sleep)
    return build_app()


async def _sync(app: Any) -> None:
    until = _START + timedelta(milliseconds=_STEP_MS * (_BAR_COUNT - 1))
    await app.call_tool(
        "sync_history",
        {
            "exchange": _EXCHANGE,
            "symbol": _SYMBOL,
            "timeframe": _TIMEFRAME,
            "since": _START.isoformat(),
            "until": until.isoformat(),
        },
    )


def _read_text(contents: Any) -> str:
    """Extract the single text payload from a FastMCP ``read_resource`` result."""
    items = list(contents)
    assert len(items) == 1
    payload = items[0].content
    assert isinstance(payload, str)
    return payload


# --------------------------------------------------------------------------- #
# Resource advertisement
# --------------------------------------------------------------------------- #
async def test_catalog_resource_is_listed() -> None:
    app = build_app()
    uris = {str(r.uri) for r in await app.list_resources()}
    assert CATALOG_URI in uris


async def test_dataset_template_is_listed() -> None:
    app = build_app()
    templates = {t.uriTemplate for t in await app.list_resource_templates()}
    assert DATASET_URI_TEMPLATE in templates


# --------------------------------------------------------------------------- #
# Catalog resource reads back the cache as CatalogResult JSON
# --------------------------------------------------------------------------- #
async def test_catalog_resource_empty_cache(app_with_data: Any) -> None:
    """Before any sync the catalog reads as a well-typed empty CatalogResult."""
    payload = _read_text(await app_with_data.read_resource(CATALOG_URI))
    result = CatalogResult.model_validate_json(payload)
    assert result.count == 0
    assert result.datasets == []


async def test_catalog_resource_lists_synced_dataset(app_with_data: Any) -> None:
    await _sync(app_with_data)
    payload = _read_text(await app_with_data.read_resource(CATALOG_URI))
    # The catalog is a typed CatalogResult: it round-trips cleanly (each entry is a
    # CatalogEntry = DatasetInfo + a typed resource_uri), so validate it directly.
    result = CatalogResult.model_validate_json(payload)
    assert result.count == 1
    entry = result.datasets[0]
    assert entry.symbol == _SYMBOL  # canonical, recovered from sidecar
    assert entry.exchange == _EXCHANGE
    assert entry.timeframe == _TIMEFRAME
    # Each entry carries a resolvable per-dataset resource URI (sanitized symbol).
    assert entry.resource_uri == f"dataset://{_EXCHANGE}/{_SANITIZED}/{_TIMEFRAME}"


# --------------------------------------------------------------------------- #
# Per-dataset template resolves a sanitized URI to canonical coverage JSON
# --------------------------------------------------------------------------- #
async def test_dataset_resource_returns_inspection(app_with_data: Any) -> None:
    await _sync(app_with_data)
    uri = f"dataset://{_EXCHANGE}/{_SANITIZED}/{_TIMEFRAME}"
    payload = _read_text(await app_with_data.read_resource(uri))
    inspection = DatasetInspection.model_validate_json(payload)
    assert inspection.symbol == _SYMBOL  # resolved back to canonical
    assert inspection.exchange == _EXCHANGE
    assert inspection.timeframe == _TIMEFRAME
    assert inspection.row_count == _BAR_COUNT
    assert inspection.monotonic_ok is True


async def test_dataset_resource_unknown_dataset_errors(app_with_data: Any) -> None:
    """An unresolvable per-dataset URI raises (redacted) rather than returning junk."""
    uri = f"dataset://{_EXCHANGE}/NOPE-USD/{_TIMEFRAME}"
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app_with_data.read_resource(uri)
    assert "NOPE-USD" in str(excinfo.value) or "No cached dataset" in str(excinfo.value)
