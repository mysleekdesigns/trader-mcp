"""Tests for the Phase 2 MCP historical data-sync tool surface.

These tools wrap the data-pipeline public API (``sync_history`` /
``list_cached_datasets`` / ``inspect_dataset``) and return typed Pydantic v2
models, so every structured tool output must validate against its declared model.
Fully offline: the paginating fake from :mod:`tests.data._ohlcv_fakes` is injected
behind the adapter's single client-construction seam (so bars flow through the real
adapter normalization), and the cache is rooted at a tmp ``data_dir`` via
``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()`` (the same env/cache pattern
``tests/test_config.py`` uses).

The end-to-end path asserted here is the Phase 2 contract: sync downloads bars into
the local cache (status ok, bars added), ``list_cached_datasets`` then surfaces the
dataset with its CANONICAL ``BTC/USD`` symbol (recovered from the sidecar manifest,
not the sanitized on-disk form), and ``inspect_dataset`` returns its coverage.
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
from trader_mcp.data import DatasetInspection, SyncResult
from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.server.app import build_app
from trader_mcp.server.schemas import DatasetsResult

_TIMEFRAME = "1h"
_SYMBOL = "BTC/USD"
_EXCHANGE = "coinbase"
_STEP_MS = timeframe_ms(_TIMEFRAME)
_BAR_COUNT = 600  # >1 page at page_cap=300 so pagination is exercised
_START = datetime(2023, 1, 1, tzinfo=UTC)
_START_MS = int(_START.timestamp() * 1000)


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-output dict from a FastMCP ``call_tool`` result."""
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Root the OHLCV cache at a tmp dir for the duration of a test.

    Sets ``TRADER_MCP_DATA_DIR`` and clears the ``get_settings`` cache both before
    and after so ``build_app``'s process-wide ``OHLCVStore()`` picks up the tmp
    location and the cache never leaks across tests.
    """
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def app_with_paginating_fake(
    monkeypatch: pytest.MonkeyPatch,
    no_credentials: None,
    data_dir: Path,
) -> Any:
    """A built app whose adapter wraps a paginating synthetic-OHLCV fake."""
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


async def _sync_btc(app: Any) -> SyncResult:
    """Sync the full synthetic BTC/USD 1h window and return the typed result."""
    until = _START + timedelta(milliseconds=_STEP_MS * (_BAR_COUNT - 1))
    structured = _structured(
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
    )
    return SyncResult.model_validate(structured)


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
PHASE2_TOOLS = ("sync_history", "list_cached_datasets", "inspect_dataset")


async def test_phase2_tools_registered() -> None:
    app = build_app()
    names = {t.name for t in await app.list_tools()}
    assert set(PHASE2_TOOLS) <= names


async def test_phase2_tools_advertise_structured_output_schema() -> None:
    app = build_app()
    by_name = {t.name: t for t in await app.list_tools()}
    for name in PHASE2_TOOLS:
        tool = by_name[name]
        assert tool.outputSchema is not None, f"{name} must advertise an outputSchema"
        assert tool.outputSchema.get("type") == "object"


async def test_sync_and_inspect_tools_constrain_exchange_enum() -> None:
    """Every Phase 2 tool taking ``exchange`` constrains it to the supported ids."""
    app = build_app()
    by_name = {t.name: t for t in await app.list_tools()}
    expected = {"coinbase", "kraken", "gemini", "cryptocom"}
    for name in ("sync_history", "inspect_dataset"):
        props = by_name[name].inputSchema.get("properties", {})
        exch = props.get("exchange")
        assert isinstance(exch, dict)
        assert set(exch.get("enum", [])) == expected, f"{name} exchange enum was {exch}"


# --------------------------------------------------------------------------- #
# End-to-end: sync -> list -> inspect (the Phase 2 tool contract)
# --------------------------------------------------------------------------- #
async def test_sync_history_downloads_into_cache(app_with_paginating_fake: Any) -> None:
    result = await _sync_btc(app_with_paginating_fake)
    assert result.status == "ok"
    assert result.bars_added > 0
    assert result.bars_total == _BAR_COUNT
    assert result.exchange == _EXCHANGE
    assert result.symbol == _SYMBOL
    assert result.pages_fetched >= 2  # page_cap=300 over 600 bars => paginated


async def test_list_cached_datasets_shows_canonical_symbol(
    app_with_paginating_fake: Any,
) -> None:
    await _sync_btc(app_with_paginating_fake)
    structured = _structured(await app_with_paginating_fake.call_tool("list_cached_datasets", {}))
    result = DatasetsResult.model_validate(structured)
    assert result.count == len(result.datasets) == 1
    info = result.datasets[0]
    # The CANONICAL symbol is recovered from the sidecar, not the on-disk form.
    assert info.symbol == _SYMBOL
    assert info.exchange == _EXCHANGE
    assert info.timeframe == _TIMEFRAME
    assert info.row_count == _BAR_COUNT


async def test_inspect_dataset_returns_coverage(app_with_paginating_fake: Any) -> None:
    await _sync_btc(app_with_paginating_fake)
    structured = _structured(
        await app_with_paginating_fake.call_tool(
            "inspect_dataset",
            {"exchange": _EXCHANGE, "symbol": _SYMBOL, "timeframe": _TIMEFRAME},
        )
    )
    inspection = DatasetInspection.model_validate(structured)
    assert inspection.symbol == _SYMBOL
    assert inspection.row_count == _BAR_COUNT
    assert inspection.monotonic_ok is True
    assert inspection.missing_bars == 0
    assert inspection.gaps == []


async def test_list_cached_datasets_empty_before_any_sync(
    app_with_paginating_fake: Any,
) -> None:
    """A fresh tmp cache reports an empty, well-typed catalog."""
    structured = _structured(await app_with_paginating_fake.call_tool("list_cached_datasets", {}))
    result = DatasetsResult.model_validate(structured)
    assert result.count == 0
    assert result.datasets == []


# --------------------------------------------------------------------------- #
# Input validation: caller errors raise (redacted) before any network
# --------------------------------------------------------------------------- #
async def test_sync_history_rejects_unsupported_timeframe(
    app_with_paginating_fake: Any,
) -> None:
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app_with_paginating_fake.call_tool(
            "sync_history",
            {
                "exchange": _EXCHANGE,
                "symbol": _SYMBOL,
                "timeframe": "3m",  # not in SUPPORTED_TIMEFRAMES
                "since": _START.isoformat(),
            },
        )
    assert "3m" in str(excinfo.value) or "timeframe" in str(excinfo.value).lower()
