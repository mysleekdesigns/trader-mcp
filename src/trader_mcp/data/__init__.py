"""Historical data pipeline & cache (owned by ``data-pipeline-engineer``).

DuckDB + Parquet OHLCV store, paginated/resumable history sync, gap detection and
repair, multi-timeframe resampling, and a dataset catalog. The MCP-server engineer
wraps :func:`sync_history`, :func:`list_cached_datasets`, and
:func:`inspect_dataset` as tools and exposes cached datasets as resources -- this
package registers **no** MCP tools/resources itself (that is the server lane).

Public surface:
    * :class:`OHLCVStore` -- DuckDB+Parquet persistence (upsert/read/list/inspect).
    * :func:`sync_history` -- the paginated, resumable, gap-repairing downloader.
    * :func:`list_cached_datasets` / :func:`inspect_dataset` -- typed catalog reads.
    * :func:`resample` -- multi-timeframe OHLCV aggregation.
    * Typed models: :class:`DatasetKey`, :class:`Gap`, :class:`DatasetInfo`,
      :class:`DatasetInspection`, :class:`SyncResult`.
    * Timeframe helpers: :func:`timeframe_ms`, :func:`is_supported_timeframe`,
      :data:`SUPPORTED_TIMEFRAMES`.

Canonical bar/result types are reused from :mod:`trader_mcp.exchanges.models`
(:class:`OHLCVBar` / :class:`OHLCVResult`); this package never redefines a bar.
"""

from __future__ import annotations

from trader_mcp.data.models import (
    DatasetInfo,
    DatasetInspection,
    DatasetKey,
    Gap,
    SyncResult,
)
from trader_mcp.data.quality import detect_gaps, expected_bar_count, is_monotonic, normalize
from trader_mcp.data.resample import resample
from trader_mcp.data.store import OHLCVStore
from trader_mcp.data.sync import sync_history
from trader_mcp.data.timeframes import (
    SUPPORTED_TIMEFRAMES,
    is_supported_timeframe,
    timeframe_ms,
)


def list_cached_datasets(store: OHLCVStore) -> list[DatasetInfo]:
    """Return a summary of every cached dataset in ``store`` (catalog listing).

    Thin, typed wrapper over :meth:`OHLCVStore.list_datasets` -- the function the
    MCP server wraps for the ``list_cached_datasets`` tool / dataset resources.
    """
    return store.list_datasets()


def inspect_dataset(store: OHLCVStore, key: DatasetKey) -> DatasetInspection:
    """Return a coverage/gap report for one cached dataset.

    Reads the full series for ``key``, normalizes it (dedupe/sort/UTC), then
    computes coverage: expected vs actual bar counts, contiguous gap runs,
    duplicates removed, and whether the stored series is monotonic. This is the
    typed result the MCP server wraps for the ``inspect_dataset`` tool.

    Args:
        store: The store to read from.
        key: The dataset to inspect.

    Returns:
        A :class:`DatasetInspection`. For an absent/empty dataset, counts are zero
        and ``gaps`` is empty.
    """
    raw = store.read_bars(key).bars
    normalized, duplicates_removed = normalize(raw, key.timeframe)
    gaps = detect_gaps(normalized, key.timeframe)
    expected = expected_bar_count(normalized, key.timeframe)
    row_count = len(normalized)
    start = normalized[0].timestamp if normalized else None
    end = normalized[-1].timestamp if normalized else None
    return DatasetInspection(
        exchange=key.exchange,
        symbol=key.symbol,
        timeframe=key.timeframe,
        row_count=row_count,
        expected_bars=expected,
        missing_bars=max(0, expected - row_count),
        gaps=gaps,
        duplicates_removed=duplicates_removed,
        monotonic_ok=is_monotonic(normalized),
        start=start,
        end=end,
    )


__all__ = [
    "SUPPORTED_TIMEFRAMES",
    "DatasetInfo",
    "DatasetInspection",
    "DatasetKey",
    "Gap",
    "OHLCVStore",
    "SyncResult",
    "detect_gaps",
    "expected_bar_count",
    "inspect_dataset",
    "is_monotonic",
    "is_supported_timeframe",
    "list_cached_datasets",
    "normalize",
    "resample",
    "sync_history",
    "timeframe_ms",
]
