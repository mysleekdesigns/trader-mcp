"""Phase 1 market-data & discovery MCP tools.

Registers the read-only, market-data-only tool surface on the FastMCP server. These
tools never place, cancel, or route orders -- they are pure data lookups, so the
safe-by-default invariant holds trivially (there is no execution path to gate).

Every tool takes typed Pydantic-validated arguments and returns a Pydantic v2 model
with ``structured_output=True`` so FastMCP emits an ``outputSchema``. List-returning
adapter methods are wrapped in the object result models from
:mod:`trader_mcp.server.schemas` (FastMCP requires a top-level object for
structured output).

Tool bodies are intentionally thin: resolve the cached adapter from the shared
:class:`~trader_mcp.exchanges.ExchangeManager`, call exactly one adapter method, and
return the typed model. The adapter raises only redacted ``TraderMCPError`` subclasses
(``ExchangeError`` / ``ValidationError``); those propagate and FastMCP surfaces the
redacted message cleanly. The ``ExchangeId`` literal constrains ``exchange`` at the
JSON-Schema level, so unsupported exchanges are rejected before a tool body runs.
"""

from __future__ import annotations

from datetime import datetime

from trader_mcp.config import ExchangeId
from trader_mcp.exchanges import (
    ExchangeCapabilities,
    ExchangeManager,
    FundingRate,
    MarketType,
    OHLCVResult,
    OrderBook,
    RecentTradesResult,
    Ticker,
    list_supported,
)
from trader_mcp.server._sdk import FastMCP
from trader_mcp.server.schemas import (
    ExchangesResult,
    MarketsResult,
    SymbolSearchResult,
)


def register_market_data_tools(app: FastMCP, manager: ExchangeManager) -> None:
    """Register the Phase 1 market-data tools on ``app``.

    All tool callables close over the single process-wide ``manager`` so concurrent
    callers share one rate-limited CCXT client per exchange. This is the only place
    these tools are registered; ``build_app`` is the single registration point that
    invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        manager: The shared exchange-adapter cache the tools resolve adapters from.
    """

    @app.tool(
        name="list_exchanges",
        title="List supported exchanges",
        description=(
            "List the exchanges trader-mcp supports, with static metadata "
            "(reliability tier, reference flag, WebSocket support). No network call."
        ),
        structured_output=True,
    )
    def list_exchanges() -> ExchangesResult:
        """Return the static registry of supported exchanges."""
        exchanges = list_supported()
        return ExchangesResult(exchanges=exchanges, count=len(exchanges))

    @app.tool(
        name="get_exchange_capabilities",
        title="Get exchange capabilities",
        description=(
            "Report what an exchange supports (spot/swap, WebSocket, OHLCV, order "
            "book, trades, funding rate, testnet) plus its loaded market count."
        ),
        structured_output=True,
    )
    async def get_exchange_capabilities(exchange: ExchangeId) -> ExchangeCapabilities:
        """Return an exchange's capability flags and market count."""
        adapter = await manager.get(exchange)
        # Populate ``market_count`` in the returned capabilities.
        await adapter.load_markets()
        return adapter.capabilities()

    @app.tool(
        name="search_symbols",
        title="Search markets",
        description=(
            "Case-insensitive substring search over an exchange's active markets "
            "(matches symbol, base, or quote). Optionally restrict to spot or swap."
        ),
        structured_output=True,
    )
    async def search_symbols(
        exchange: ExchangeId,
        query: str,
        market_type: MarketType | None = None,
        limit: int = 50,
    ) -> SymbolSearchResult:
        """Search an exchange's markets for symbols matching ``query``."""
        adapter = await manager.get(exchange)
        markets = await adapter.search_symbols(query, market_type=market_type, limit=limit)
        return SymbolSearchResult(
            exchange=exchange,
            query=query,
            markets=markets,
            count=len(markets),
        )

    @app.tool(
        name="list_markets",
        title="List markets",
        description=(
            "List an exchange's markets, optionally filtered by type (spot/swap) and "
            "active status, and capped by ``limit``."
        ),
        structured_output=True,
    )
    async def list_markets(
        exchange: ExchangeId,
        market_type: MarketType | None = None,
        active_only: bool = True,
        limit: int | None = None,
    ) -> MarketsResult:
        """List an exchange's markets after optional type/active filtering."""
        adapter = await manager.get(exchange)
        markets = await adapter.list_markets(
            market_type=market_type,
            active_only=active_only,
            limit=limit,
        )
        return MarketsResult(exchange=exchange, markets=markets, count=len(markets))

    @app.tool(
        name="get_ticker",
        title="Get ticker",
        description="Fetch a normalized ticker snapshot (last/bid/ask/volume) for a symbol.",
        structured_output=True,
    )
    async def get_ticker(exchange: ExchangeId, symbol: str) -> Ticker:
        """Fetch the latest ticker for ``symbol`` on ``exchange``."""
        adapter = await manager.get(exchange)
        return await adapter.fetch_ticker(symbol)

    @app.tool(
        name="get_ohlcv",
        title="Get OHLCV candles",
        description=(
            "Fetch OHLCV candles for a symbol/timeframe. Defaults to the 200 most "
            "recent 1h bars; pass ``since`` (UTC) to start from a given open time."
        ),
        structured_output=True,
    )
    async def get_ohlcv(
        exchange: ExchangeId,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 200,
        since: datetime | None = None,
    ) -> OHLCVResult:
        """Fetch OHLCV candles for ``symbol``/``timeframe`` on ``exchange``."""
        adapter = await manager.get(exchange)
        return await adapter.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    @app.tool(
        name="get_order_book",
        title="Get order book",
        description="Fetch a normalized order-book snapshot (bids/asks) for a symbol.",
        structured_output=True,
    )
    async def get_order_book(exchange: ExchangeId, symbol: str, limit: int = 25) -> OrderBook:
        """Fetch the top-``limit`` order-book levels for ``symbol``."""
        adapter = await manager.get(exchange)
        return await adapter.fetch_order_book(symbol, limit=limit)

    @app.tool(
        name="get_recent_trades",
        title="Get recent trades",
        description="Fetch the most recent public trade prints for a symbol.",
        structured_output=True,
    )
    async def get_recent_trades(
        exchange: ExchangeId, symbol: str, limit: int = 50
    ) -> RecentTradesResult:
        """Fetch up to ``limit`` recent public trades for ``symbol``."""
        adapter = await manager.get(exchange)
        return await adapter.fetch_recent_trades(symbol, limit=limit)

    @app.tool(
        name="get_funding_rate",
        title="Get funding rate",
        description=(
            "Fetch the current perpetual-swap funding-rate snapshot for a symbol "
            "(rate, next funding time, mark/index price)."
        ),
        structured_output=True,
    )
    async def get_funding_rate(exchange: ExchangeId, symbol: str) -> FundingRate:
        """Fetch the funding-rate snapshot for a perpetual-swap ``symbol``."""
        adapter = await manager.get(exchange)
        return await adapter.fetch_funding_rate(symbol)
