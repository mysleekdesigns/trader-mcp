"""FastMCP application factory and Phase 0 admin tools.

Builds the :class:`FastMCP` server (via the SDK-isolation wrapper in
:mod:`trader_mcp.server._sdk`) and registers two trivial, fully-typed tools:
``health_check`` and ``get_server_status``. ``mcp-server-engineer`` extends the
tool surface from Phase 1; keep this factory as the single registration point.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

from trader_mcp import __version__
from trader_mcp.logging_config import get_logger
from trader_mcp.server._sdk import FastMCP, create_fastmcp
from trader_mcp.server.schemas import HealthCheckResult, ServerStatusResult

logger = get_logger(__name__)

#: Server name advertised to MCP clients.
SERVER_NAME = "trader-mcp"

#: v1 transport (PRD §4): stdio. Remote Streamable HTTP is a future phase.
TRANSPORT: Literal["stdio"] = "stdio"

_SERVER_INSTRUCTIONS = (
    "trader-mcp is an AI-native crypto trading platform. It exposes typed tools to "
    "research market data, author declarative strategies, backtest on real cached "
    "data, and paper/testnet trade across Bybit, BloFin, Toobit, and WeeX. "
    "Execution is dry-run and safe-by-default; real-money trading is gated."
)


def build_app() -> FastMCP:
    """Build and return the configured FastMCP application.

    Registers the Phase 0 admin tools. The process start time is captured at build
    time so ``get_server_status`` can report uptime.

    Returns:
        A :class:`FastMCP` instance ready to ``run(transport=...)``.
    """
    app = create_fastmcp(SERVER_NAME, instructions=_SERVER_INSTRUCTIONS)
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

    logger.debug("Built FastMCP app '%s' with admin tools registered.", SERVER_NAME)
    return app
