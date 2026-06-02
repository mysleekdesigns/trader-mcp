"""Phase 2 dataset MCP resources (PRD §6: expose cached datasets as resources).

Two resources are registered against the FastMCP server:

    * ``dataset://catalog`` -- a single resource returning the JSON of
      :class:`~trader_mcp.server.schemas.CatalogResult` (every cached dataset,
      with canonical symbols and a resolvable per-dataset resource URI). It is a
      richer, self-validating superset of the ``list_cached_datasets`` tool's
      ``DatasetsResult`` (each entry adds a typed ``resource_uri``).
    * ``dataset://{exchange}/{symbol}/{timeframe}`` -- a resource template
      returning one dataset's :class:`~trader_mcp.data.DatasetInspection` JSON.

URI caveat: CCXT symbols contain ``/`` and ``:`` (``BTC/USD``, ``BTC/USD:USD``),
which are not URI-path-safe. The store sanitizes them on disk (``/``/``:`` -> ``-``)
and recovers the canonical symbol from a sidecar manifest. The per-dataset
template therefore accepts the **sanitized** symbol form in the URI
(``dataset://coinbase/BTC-USD/1h``) and resolves it back to the canonical
:class:`~trader_mcp.data.DatasetKey` by scanning ``store.list_datasets()`` and
matching on sanitized components. The catalog embeds each dataset's resolvable URI
so clients can discover the exact form to read.

These resources are read-only over the local cache -- no network, no order/arming
path. Resource registration goes through the FastMCP instance (re-exported behind
:mod:`trader_mcp.server._sdk`), so the SDK stays isolated.
"""

from __future__ import annotations

from trader_mcp import data
from trader_mcp.data import DatasetInfo, DatasetKey, OHLCVStore
from trader_mcp.data.store import sanitize_symbol
from trader_mcp.errors import ValidationError
from trader_mcp.server._sdk import FastMCP
from trader_mcp.server.schemas import CatalogEntry, CatalogResult

#: URI of the cache catalog resource.
CATALOG_URI = "dataset://catalog"

#: URI template for a single dataset's coverage report. ``{symbol}`` is the
#: **sanitized** form (``/`` and ``:`` collapsed to ``-``).
DATASET_URI_TEMPLATE = "dataset://{exchange}/{symbol}/{timeframe}"


def dataset_uri(info: DatasetInfo) -> str:
    """Return the resolvable per-dataset resource URI for ``info`` (sanitized symbol)."""
    return f"dataset://{info.exchange}/{sanitize_symbol(info.symbol)}/{info.timeframe}"


def _resolve_key(store: OHLCVStore, exchange: str, symbol: str, timeframe: str) -> DatasetKey:
    """Resolve a (possibly sanitized) URI triple to a canonical :class:`DatasetKey`.

    Scans the cache catalog and matches on sanitized components so a URI carrying
    the on-disk symbol form (``BTC-USD``) recovers the canonical CCXT symbol
    (``BTC/USD``). Raises a redacted :class:`ValidationError` when no dataset
    matches (the message never echoes anything but the requested, already-safe
    URI components).
    """
    want = sanitize_symbol(symbol)
    for info in store.list_datasets():
        if (
            info.exchange == exchange
            and info.timeframe == timeframe
            and sanitize_symbol(info.symbol) == want
        ):
            return DatasetKey(exchange=info.exchange, symbol=info.symbol, timeframe=info.timeframe)
    raise ValidationError(f"No cached dataset matches dataset://{exchange}/{symbol}/{timeframe}")


def register_dataset_resources(app: FastMCP, store: OHLCVStore) -> None:
    """Register the Phase 2 dataset resources on ``app``.

    Both resource callables close over the single process-wide ``store``. This is
    the only place these resources are registered; ``build_app`` invokes it.

    Args:
        app: The FastMCP application to register the resources on.
        store: The shared local OHLCV cache the resources read from.
    """

    @app.resource(
        CATALOG_URI,
        name="dataset-catalog",
        title="Cached dataset catalog",
        description=(
            "JSON catalog of every cached OHLCV dataset (canonical symbols, coverage "
            "windows, row counts) with a resolvable per-dataset resource URI for each."
        ),
        mime_type="application/json",
    )
    def catalog() -> str:
        """Return the cache catalog as ``CatalogResult`` JSON (each entry URI-tagged)."""
        datasets = data.list_cached_datasets(store)
        entries = [
            CatalogEntry(**info.model_dump(), resource_uri=dataset_uri(info)) for info in datasets
        ]
        return CatalogResult(datasets=entries, count=len(entries)).model_dump_json()

    @app.resource(
        DATASET_URI_TEMPLATE,
        name="dataset-coverage",
        title="Cached dataset coverage",
        description=(
            "JSON coverage/quality report (DatasetInspection) for one cached dataset. "
            "The symbol path segment is the sanitized form, e.g. "
            "'dataset://coinbase/BTC-USD/1h'; it is resolved back to the canonical "
            "symbol via the cache catalog."
        ),
        mime_type="application/json",
    )
    def dataset_coverage(exchange: str, symbol: str, timeframe: str) -> str:
        """Return one dataset's ``DatasetInspection`` JSON, resolved from a sanitized URI."""
        key = _resolve_key(store, exchange, symbol, timeframe)
        inspection = data.inspect_dataset(store, key)
        return inspection.model_dump_json()
