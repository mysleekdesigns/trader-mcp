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
from trader_mcp.exchanges import ExchangeInfo, Market


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
        description="Metadata for every supported exchange (Bybit, the reference, first)."
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
