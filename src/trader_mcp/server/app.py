"""FastMCP application factory, admin tools, and lifecycle wiring.

Builds the :class:`FastMCP` server (via the SDK-isolation wrapper in
:mod:`trader_mcp.server._sdk`) and registers the fully-typed tool surface:

    * the Phase 0 admin tools ``health_check`` and ``get_server_status``;
    * the Phase 1 market-data & discovery tools (registered via
      :func:`trader_mcp.server.market_data.register_market_data_tools`).

``build_app`` is the single registration point. It also constructs exactly one
process-wide :class:`~trader_mcp.exchanges.ExchangeManager` and wires a FastMCP
lifespan that closes its cached adapters on shutdown -- this keeps the SDK behind
``_sdk`` (the lifespan is passed through ``create_fastmcp``).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from trader_mcp import __version__
from trader_mcp.exchanges import ExchangeManager
from trader_mcp.logging_config import get_logger
from trader_mcp.server._sdk import FastMCP, create_fastmcp
from trader_mcp.server.market_data import register_market_data_tools
from trader_mcp.server.schemas import HealthCheckResult, ServerStatusResult

logger = get_logger(__name__)

#: Server name advertised to MCP clients.
SERVER_NAME = "trader-mcp"

#: v1 transport (PRD §4): stdio. Remote Streamable HTTP is a future phase.
TRANSPORT: Literal["stdio"] = "stdio"

_SERVER_INSTRUCTIONS = (
    "trader-mcp is an AI-native crypto trading platform. It exposes typed tools to "
    "research market data, author declarative strategies, backtest on real cached "
    "data, and paper/testnet trade across Coinbase, Kraken, Gemini, and Crypto.com. "
    "Execution is dry-run and safe-by-default; real-money trading is gated."
)


def build_app() -> FastMCP:
    """Build and return the configured FastMCP application.

    Registers the Phase 0 admin tools and the Phase 1 market-data tools. Constructs
    exactly one process-wide :class:`~trader_mcp.exchanges.ExchangeManager`, closed
    over by the market-data tool callables, and wires a FastMCP lifespan that calls
    ``manager.aclose_all()`` on shutdown (the transport runners enter/exit the
    lifespan; ``build_app``/``list_tools`` do not, so no client is created until a
    tool actually runs). The process start time is captured at build time so
    ``get_server_status`` can report uptime.

    Returns:
        A :class:`FastMCP` instance ready to ``run(transport=...)``.
    """
    manager = ExchangeManager()

    @asynccontextmanager
    async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
        """Bracket the serving lifetime; close cached adapters on shutdown."""
        try:
            yield
        finally:
            await manager.aclose_all()

    app = create_fastmcp(
        SERVER_NAME,
        instructions=_SERVER_INSTRUCTIONS,
        lifespan=_lifespan,
    )
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()

    @app.tool(
        name="health_check",
        title="Health check",
        description="Liveness probe: returns 'ok', the server version, and a timestamp.",
        structured_output=True,
    )
    def health_check() -> HealthCheckResult:
        """Return a minimal liveness result."""
        return HealthCheckResult(
            status="ok",
            version=__version__,
            timestamp=datetime.now(UTC),
        )

    @app.tool(
        name="get_server_status",
        title="Server status",
        description=(
            "Return server metadata: name, version, transport, start time, uptime, "
            "and the number of registered tools."
        ),
        structured_output=True,
    )
    async def get_server_status() -> ServerStatusResult:
        """Return server metadata including the live registered-tool count."""
        tools = await app.list_tools()
        return ServerStatusResult(
            name=SERVER_NAME,
            version=__version__,
            transport=TRANSPORT,
            started_at=started_at,
            uptime_seconds=round(time.monotonic() - started_monotonic, 3),
            tool_count=len(tools),
        )

    register_market_data_tools(app, manager)

    logger.debug("Built FastMCP app '%s' with admin + market-data tools registered.", SERVER_NAME)
    return app
