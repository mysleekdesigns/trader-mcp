"""Typed Pydantic v2 I/O models for the MCP tool surface.

Every tool takes a typed input and returns a typed output so FastMCP can emit an
``outputSchema`` / structured content. These models cover the Phase 0 admin tools
plus the wrapper result models for the Phase 1 market-data tools whose adapter
methods return *lists* -- FastMCP structured output requires a top-level object,
so a list result is wrapped in an object carrying the list plus a ``count`` (and,
where useful, the query echo).

The six market-data tools whose adapter methods already return a single domain
model (ticker, OHLCV, order book, recent trades, funding rate, capabilities)
return those :mod:`trader_mcp.exchanges` models directly and need no wrapper here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from trader_mcp.config import ExchangeId
from trader_mcp.data import DatasetInfo
from trader_mcp.exchanges import ExchangeInfo, Market
from trader_mcp.indicators import IndicatorInfo
from trader_mcp.strategy import StrategyInfo, StrategySpec, TemplateInfo


class _StrictModel(BaseModel):
    """Base model: forbid unexpected fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class HealthCheckResult(_StrictModel):
    """Result of the ``health_check`` tool."""

    status: str = Field(description="Liveness status; 'ok' when the server is up.")
    version: str = Field(description="trader-mcp package version.")
    timestamp: datetime = Field(description="UTC time the check was produced.")


class ServerStatusResult(_StrictModel):
    """Result of the ``get_server_status`` tool."""

    name: str = Field(description="Server name advertised to MCP clients.")
    version: str = Field(description="trader-mcp package version.")
    transport: str = Field(description="Active MCP transport (e.g. 'stdio').")
    started_at: datetime = Field(description="UTC time the server process started.")
    uptime_seconds: float = Field(description="Seconds since the server started.")
    tool_count: int = Field(description="Number of registered MCP tools.")


# --------------------------------------------------------------------------- #
# Phase 1 market-data list wrappers
#
# FastMCP structured output requires an *object* at the top level, so the three
# tools backed by list-returning adapter methods (``list_exchanges``,
# ``search_symbols``, ``list_markets``) return these wrappers instead of a bare
# list. The single-model tools return the exchanges domain models directly.
# --------------------------------------------------------------------------- #
class ExchangesResult(_StrictModel):
    """Result of ``list_exchanges``: the static supported-exchange registry."""

    exchanges: list[ExchangeInfo] = Field(
        description="Metadata for every supported exchange (Coinbase, the reference, first)."
    )
    count: int = Field(description="Number of supported exchanges.")


class SymbolSearchResult(_StrictModel):
    """Result of ``search_symbols``: markets matching a substring query."""

    exchange: ExchangeId = Field(description="Exchange the search ran against.")
    query: str = Field(description="Substring matched against symbol/base/quote.")
    markets: list[Market] = Field(description="Matching markets (capped by ``limit``).")
    count: int = Field(description="Number of markets returned.")


class MarketsResult(_StrictModel):
    """Result of ``list_markets``: the (optionally filtered) market list."""

    exchange: ExchangeId = Field(description="Exchange the markets belong to.")
    markets: list[Market] = Field(description="Markets after type/active filtering and limit.")
    count: int = Field(description="Number of markets returned.")


# --------------------------------------------------------------------------- #
# Phase 2 historical-data list wrapper
#
# ``list_cached_datasets`` wraps the data layer's list-returning catalog read.
# As with the market-data wrappers, FastMCP structured output needs a top-level
# object, so the bare ``list[DatasetInfo]`` is wrapped here with a ``count``.
# --------------------------------------------------------------------------- #
class DatasetsResult(_StrictModel):
    """Result of ``list_cached_datasets``: the local OHLCV cache catalog."""

    datasets: list[DatasetInfo] = Field(
        description="Summary of every cached dataset (exchange, canonical symbol, timeframe, "
        "row count, coverage window, last sync time)."
    )
    count: int = Field(description="Number of cached datasets.")


# --------------------------------------------------------------------------- #
# Phase 2 dataset-catalog resource models
#
# The ``dataset://catalog`` MCP resource returns a *richer* view than the
# ``list_cached_datasets`` tool: each entry carries the resolvable per-dataset
# resource URI so clients can discover the exact ``dataset://...`` to read. This
# is a distinct, self-validating schema (``CatalogEntry`` extends ``DatasetInfo``
# with a typed ``resource_uri``) so the catalog payload round-trips through
# ``CatalogResult`` -- the tool keeps returning the strict ``DatasetsResult``.
# --------------------------------------------------------------------------- #
class CatalogEntry(DatasetInfo):
    """A cache-catalog entry: a :class:`DatasetInfo` plus its resolvable resource URI."""

    resource_uri: str = Field(
        description="Resolvable per-dataset resource URI (sanitized symbol form), e.g. "
        "'dataset://coinbase/BTC-USD/1h'."
    )


class CatalogResult(_StrictModel):
    """Payload of the ``dataset://catalog`` resource: URI-tagged cache entries."""

    datasets: list[CatalogEntry] = Field(
        description="Every cached dataset with its resolvable per-dataset resource URI."
    )
    count: int = Field(description="Number of cached datasets.")


# --------------------------------------------------------------------------- #
# Phase 3 strategy-authoring list wrappers + result models
#
# The strategy package returns *lists* (templates, saved strategies, the
# indicator whitelist). FastMCP structured output needs a top-level object, so
# each list result is wrapped here with a ``count``. ``CreateStrategyResult`` and
# ``DeleteResult`` carry a small object payload for the create/delete tools; the
# ``StrategySpec`` and ``ValidationReport`` are already object models and are
# returned directly by the get/validate tools.
# --------------------------------------------------------------------------- #
class TemplatesResult(_StrictModel):
    """Result of ``list_strategy_templates``: the starter template library."""

    templates: list[TemplateInfo] = Field(
        description="Metadata for every strategy template (id, title, summary, "
        "strategy_type, overridable fields) -- starting points for authoring."
    )
    count: int = Field(description="Number of templates.")


class IndicatorsResult(_StrictModel):
    """Result of ``list_indicators``: the closed indicator whitelist."""

    indicators: list[IndicatorInfo] = Field(
        description="Every whitelisted indicator kind with its params (and defaults) and "
        "output-name suffixes. Rules may reference ONLY these indicator outputs."
    )
    count: int = Field(description="Number of whitelisted indicator kinds.")


class StrategiesResult(_StrictModel):
    """Result of ``list_strategies``: the saved-strategy catalog."""

    strategies: list[StrategyInfo] = Field(
        description="Listing summary of every saved strategy (name, exchange, symbol, "
        "timeframe, strategy_type, schema_version, created/updated timestamps)."
    )
    count: int = Field(description="Number of saved strategies.")


class CreateStrategyResult(_StrictModel):
    """Result of ``create_strategy`` / ``update_strategy``: the saved spec + its listing info."""

    spec: StrategySpec = Field(description="The validated, persisted strategy spec.")
    info: StrategyInfo = Field(
        description="The persisted listing summary (name is the identity/key; carries "
        "created/updated timestamps)."
    )


class DeleteResult(_StrictModel):
    """Result of ``delete_strategy``: which strategy was targeted and whether it existed."""

    name: str = Field(description="The strategy name that was targeted for deletion.")
    deleted: bool = Field(
        description="True if a saved strategy existed and was removed; False if none matched."
    )


# --------------------------------------------------------------------------- #
# Phase 3 strategy-catalog resource models
#
# The ``strategy://catalog`` MCP resource returns a *richer* view than the
# ``list_strategies`` tool: each entry carries the resolvable per-strategy
# resource URI (``strategy://{slug}``) so clients can discover the exact
# ``strategy://...`` to read. Mirrors the dataset-catalog idiom.
# --------------------------------------------------------------------------- #
class StrategyCatalogEntry(StrategyInfo):
    """A strategy-catalog entry: a :class:`StrategyInfo` plus its resolvable resource URI."""

    resource_uri: str = Field(
        description="Resolvable per-strategy resource URI (slugified name), e.g. "
        "'strategy://ma-cross-btc'."
    )


class StrategyCatalogResult(_StrictModel):
    """Payload of the ``strategy://catalog`` resource: URI-tagged saved strategies."""

    strategies: list[StrategyCatalogEntry] = Field(
        description="Every saved strategy with its resolvable per-strategy resource URI."
    )
    count: int = Field(description="Number of saved strategies.")
