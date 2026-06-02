"""Phase 2 historical data-sync MCP tools.

Wraps the data-pipeline public API (:func:`trader_mcp.data.sync_history`,
:func:`trader_mcp.data.list_cached_datasets`, :func:`trader_mcp.data.inspect_dataset`)
as a typed, structured-output MCP tool surface for downloading and inspecting the
local OHLCV cache.

These tools are SAFE by construction: the only network call is the read-only
``adapter.fetch_ohlcv`` paginated download; everything else writes solely to the
**local** DuckDB+Parquet cache. There is no order, arming, or credential path here
(the safe-by-default invariant holds trivially -- there is nothing to gate).

Every tool takes typed Pydantic-validated arguments and returns a Pydantic v2 model
with ``structured_output=True`` so FastMCP emits an ``outputSchema``. The data layer
already returns typed models (``SyncResult`` / ``DatasetInspection``); the only
wrapper added here is :class:`~trader_mcp.server.schemas.DatasetsResult` around the
list-returning catalog read. The ``ExchangeId`` literal constrains ``exchange`` at
the JSON-Schema level, and the data layer raises only redacted ``TraderMCPError``
subclasses (``ValidationError`` for caller input, never raw exchange/network faults
-- those surface as ``status="partial"`` with a redacted note), so errors propagate
cleanly to the client.
"""

from __future__ import annotations

from datetime import datetime

from trader_mcp import data
from trader_mcp.config import ExchangeId
from trader_mcp.data import DatasetInspection, DatasetKey, OHLCVStore, SyncResult
from trader_mcp.exchanges import ExchangeManager
from trader_mcp.server._sdk import FastMCP
from trader_mcp.server.schemas import DatasetsResult


def register_data_tools(app: FastMCP, manager: ExchangeManager, store: OHLCVStore) -> None:
    """Register the Phase 2 historical-data tools on ``app``.

    The tool callables close over the single process-wide ``manager`` (for the
    read-only download path) and the single process-wide ``store`` (the local
    cache). This is the only place these tools are registered; ``build_app`` is
    the single registration point that invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        manager: The shared exchange-adapter cache used for the read-only download.
        store: The shared local OHLCV cache the tools sync into / read from.
    """

    @app.tool(
        name="sync_history",
        title="Sync historical OHLCV",
        description=(
            "Paginated, incremental, resumable OHLCV download into the local "
            "DuckDB+Parquet cache. Read-only against the exchange (only fetches "
            "candles -- no order/arming path). Re-running adds only new bars; gaps "
            "are detected and (by default) repaired. Returns a typed sync summary."
        ),
        structured_output=True,
    )
    async def sync_history(
        exchange: ExchangeId,
        symbol: str,
        timeframe: str,
        since: datetime,
        until: datetime | None = None,
        page_limit: int = 1000,
        repair_gaps: bool = True,
    ) -> SyncResult:
        """Download/refresh cached OHLCV for ``symbol``/``timeframe`` on ``exchange``."""
        return await data.sync_history(
            manager,
            exchange,
            symbol,
            timeframe,
            since=since,
            until=until,
            page_limit=page_limit,
            repair_gaps=repair_gaps,
            store=store,
        )

    @app.tool(
        name="list_cached_datasets",
        title="List cached datasets",
        description=(
            "List every dataset in the local OHLCV cache with its canonical symbol, "
            "timeframe, row count, coverage window, and last sync time. No network "
            "call -- reads the local cache only."
        ),
        structured_output=True,
    )
    def list_cached_datasets() -> DatasetsResult:
        """Return the local OHLCV cache catalog."""
        datasets = data.list_cached_datasets(store)
        return DatasetsResult(datasets=datasets, count=len(datasets))

    @app.tool(
        name="inspect_dataset",
        title="Inspect cached dataset",
        description=(
            "Coverage/quality report for one cached dataset: expected vs actual bar "
            "counts, contiguous missing-bar gaps, duplicates removed during "
            "normalization, and whether the stored series is monotonic. No network "
            "call -- reads the local cache only."
        ),
        structured_output=True,
    )
    def inspect_dataset(exchange: ExchangeId, symbol: str, timeframe: str) -> DatasetInspection:
        """Report coverage/quality for the cached ``symbol``/``timeframe`` dataset."""
        key = DatasetKey(exchange=exchange, symbol=symbol, timeframe=timeframe)
        return data.inspect_dataset(store, key)
