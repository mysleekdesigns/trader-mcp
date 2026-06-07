"""FastMCP application factory, admin tools, and lifecycle wiring.

Builds the :class:`FastMCP` server (via the SDK-isolation wrapper in
:mod:`trader_mcp.server._sdk`) and registers the fully-typed tool surface:

    * the Phase 0 admin tools ``health_check`` and ``get_server_status``;
    * the Phase 1 market-data & discovery tools (registered via
      :func:`trader_mcp.server.market_data.register_market_data_tools`);
    * the Phase 2 historical data-sync tools (registered via
      :func:`trader_mcp.server.historical.register_data_tools`) and the cached-
      dataset MCP resources (via
      :func:`trader_mcp.server.resources.register_dataset_resources`);
    * the Phase 3 strategy-authoring tools (via
      :func:`trader_mcp.server.strategy.register_strategy_tools`), the saved-
      strategy MCP resources (via
      :func:`trader_mcp.server.resources.register_strategy_resources`), and the
      guided strategy-design MCP prompts (via
      :func:`trader_mcp.server.prompts.register_strategy_prompts`);
    * the Phase 4 backtest & optimize tools (via
      :func:`trader_mcp.server.backtest.register_backtest_tools`) and the saved-
      backtest-report MCP resources (via
      :func:`trader_mcp.server.resources.register_backtest_resources`);
    * the Phase 5 paper/testnet execution tools (via
      :func:`trader_mcp.server.execution.register_execution_tools`) and the
      portfolio/analytics tools (via
      :func:`trader_mcp.server.portfolio.register_portfolio_tools`);
    * the Phase 6 guardrails-only safety tools (via
      :func:`trader_mcp.server.safety.register_safety_tools`) -- ``set_risk_limits``,
      ``arm_live_trading``, ``disarm_live_trading``, ``kill_switch``,
      ``get_safety_status``, ``get_audit_log`` -- wired against the single
      process-wide :class:`~trader_mcp.safety.SafetyController`. These configure and
      observe the safety machinery; they do NOT open the live wall (no ``live``
      session mode; ``arm_live_trading`` records state no live path consumes yet).

``build_app`` is the single registration point. It constructs exactly one
process-wide :class:`~trader_mcp.exchanges.ExchangeManager`, one process-wide
:class:`~trader_mcp.data.OHLCVStore`, one process-wide
:class:`~trader_mcp.strategy.StrategyStore`, one process-wide
:class:`~trader_mcp.engine.BacktestStore`, one process-wide
:class:`~trader_mcp.execution.SessionRegistry`, and one process-wide
:class:`~trader_mcp.safety.SafetyController`, and wires a FastMCP lifespan that
stops any running execution sessions and closes the manager's cached adapters on
shutdown -- this keeps the SDK behind ``_sdk`` (the lifespan is passed through
``create_fastmcp``). The stores use short-lived file/DuckDB handles (no persistent
connection), so they need no lifespan teardown; the session registry is in-memory.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Literal

from trader_mcp import __version__
from trader_mcp.data import OHLCVStore
from trader_mcp.engine import BacktestStore
from trader_mcp.exchanges import ExchangeManager
from trader_mcp.execution import PaperBroker, SessionRegistry
from trader_mcp.logging_config import get_logger
from trader_mcp.safety import SafetyController
from trader_mcp.server._sdk import FastMCP, create_fastmcp
from trader_mcp.server.backtest import register_backtest_tools
from trader_mcp.server.execution import register_execution_tools
from trader_mcp.server.historical import register_data_tools
from trader_mcp.server.market_data import register_market_data_tools
from trader_mcp.server.portfolio import register_portfolio_tools
from trader_mcp.server.prompts import register_strategy_prompts
from trader_mcp.server.resources import (
    register_backtest_resources,
    register_dataset_resources,
    register_strategy_resources,
)
from trader_mcp.server.safety import register_safety_tools
from trader_mcp.server.schemas import HealthCheckResult, ServerStatusResult
from trader_mcp.server.strategy import register_strategy_tools
from trader_mcp.strategy import StrategyStore

logger = get_logger(__name__)

#: Server name advertised to MCP clients.
SERVER_NAME = "trader-mcp"

#: v1 transport (PRD §4): stdio. Remote Streamable HTTP is a future phase.
TRANSPORT: Literal["stdio"] = "stdio"

_SERVER_INSTRUCTIONS = (
    "trader-mcp is an AI-native crypto trading platform. It exposes typed tools to "
    "research market data, author declarative strategies, backtest on real cached "
    "data, and paper/testnet trade across Coinbase, Kraken, Gemini, and Crypto.com. "
    "Historical OHLCV is synced into a local DuckDB+Parquet cache and exposed as MCP "
    "resources (dataset://...). Strategies are declarative, typed specs -- author them "
    "with create_strategy/validate_strategy (rules may use only whitelisted "
    "indicators/operators), browse them via list_strategies and the strategy://... "
    "resources, and start from list_strategy_templates or the design_strategy prompt. "
    "Execution is dry-run and safe-by-default; real-money trading is gated."
)


def build_app() -> FastMCP:
    """Build and return the configured FastMCP application.

    Registers the Phase 0 admin tools, the Phase 1 market-data tools, the Phase 2
    historical data-sync tools + cached-dataset resources, the Phase 3
    strategy-authoring tools + saved-strategy resources + guided design prompts,
    the Phase 4 backtest & optimize tools + saved-backtest-report resources, the
    Phase 5 paper/testnet execution tools + portfolio/analytics tools, and the
    Phase 6 guardrails-only safety tools (risk limits, arm/disarm, kill switch,
    safety status, audit log). Constructs exactly one process-wide
    :class:`~trader_mcp.exchanges.ExchangeManager`, one
    :class:`~trader_mcp.data.OHLCVStore`, one
    :class:`~trader_mcp.strategy.StrategyStore`, one
    :class:`~trader_mcp.engine.BacktestStore`, and one
    :class:`~trader_mcp.execution.SessionRegistry`, closed over by the
    tool/resource/prompt callables, and wires a FastMCP lifespan that stops any
    running execution sessions and calls ``manager.aclose_all()`` on
    shutdown (the transport runners enter/exit the lifespan; ``build_app``/
    ``list_tools`` do not, so no client is created until a tool actually runs). The
    process start time is captured at build time so ``get_server_status`` can
    report uptime.

    Returns:
        A :class:`FastMCP` instance ready to ``run(transport=...)``.
    """
    manager = ExchangeManager()
    # One process-wide local OHLCV cache; ``OHLCVStore()`` picks up
    # ``settings.data_dir`` (overridable in tests via TRADER_MCP_DATA_DIR +
    # ``get_settings.cache_clear()``). Construction is cheap and touches no I/O.
    store = OHLCVStore()
    # One process-wide local strategy store (Phase 3); same data_dir convention,
    # rooted at ``{data_dir}/strategies``. Construction touches no filesystem.
    strategy_store = StrategyStore()
    # One process-wide local backtest-report store (Phase 4); same data_dir
    # convention, rooted at ``{data_dir}/backtests``. Construction touches no
    # filesystem (the directory is created on first save).
    backtest_store = BacktestStore()
    # One process-wide in-memory execution-session registry (Phase 5). It mints
    # session ids and owns the async run-task lifecycle for paper/testnet sessions;
    # state is entirely in memory (no filesystem). The lifespan stops any running
    # sessions on shutdown so no run loop outlives the server.
    session_registry = SessionRegistry()
    # One process-wide safety controller (Phase 6): risk limits + the (expiring,
    # confirmation-gated) live-arming state machine + the kill switch + the redacted
    # audit log. ``place_order`` runs every intent through ``controller.preflight``;
    # the safety tools configure/observe it. Guardrails-only -- no live wall is opened
    # here (``arm_live_trading`` records state that no live path consumes yet).
    controller = SafetyController()
    # The MANUAL paper brokers (orders on an undeployed session) live in the execution
    # tool closure. Build the mapping HERE and share it with both lanes so the kill
    # switch's cancel-all can also reach open manual paper orders.
    manual_brokers: dict[str, PaperBroker] = {}

    @asynccontextmanager
    async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
        """Bracket the serving lifetime; stop sessions + close adapters on shutdown."""
        try:
            yield
        finally:
            for info in session_registry.list():
                with suppress(Exception):
                    await session_registry.stop(info.session_id)
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
    register_data_tools(app, manager, store)
    register_dataset_resources(app, store)
    register_strategy_tools(app, strategy_store)
    register_strategy_resources(app, strategy_store)
    register_strategy_prompts(app)
    register_backtest_tools(app, strategy_store, store, backtest_store)
    register_backtest_resources(app, backtest_store)
    register_execution_tools(
        app,
        manager,
        session_registry,
        strategy_store,
        store,
        controller,
        manual_brokers,
    )
    register_portfolio_tools(app, session_registry)
    register_safety_tools(app, controller, session_registry, manual_brokers)

    logger.debug(
        "Built FastMCP app '%s' with admin + market-data + historical + strategy + "
        "backtest + execution + portfolio + safety tools, dataset + strategy + "
        "backtest resources, and strategy prompts registered.",
        SERVER_NAME,
    )
    return app
