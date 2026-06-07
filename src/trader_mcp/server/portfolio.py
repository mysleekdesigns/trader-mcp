"""Phase 5 portfolio & analytics MCP tools (PRD §5.2 "portfolio/analytics").

Read-only views over a running (or stopped) paper/testnet session's broker: the
mark-to-market portfolio snapshot, the realized/unrealized pnl decomposition, and
the closed-trade history. These tools place no orders and touch no network -- they
only read the session's in-memory :class:`~trader_mcp.execution.PaperBroker` state
through the :class:`~trader_mcp.execution.SessionRegistry`, so the safe-by-default
invariant holds trivially (there is nothing to gate).

Typed I/O: every tool takes a Pydantic-validated ``session_id`` and returns an
execution Pydantic v2 model (:class:`~trader_mcp.execution.Portfolio` /
:class:`~trader_mcp.execution.PnLBreakdown`) or a thin object wrapper for the trade
list, with ``structured_output=True`` so FastMCP emits an ``outputSchema``.

The MCP SDK stays isolated: registration goes through the ``FastMCP`` instance
re-exported from :mod:`trader_mcp.server._sdk`; this module never ``import mcp``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from trader_mcp.execution import (
    PnLBreakdown,
    Portfolio,
    SessionRegistry,
    TradeRecord,
)
from trader_mcp.server._sdk import FastMCP


class TradeHistoryResult(BaseModel):
    """A session's closed round-trips plus their count.

    FastMCP structured output requires a top-level object, so the bare list of
    :class:`~trader_mcp.execution.TradeRecord` is wrapped here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(description="The session these trades belong to.")
    trades: list[TradeRecord] = Field(description="Closed round-trips, in close order.")
    count: int = Field(description="Number of closed trades.")


def register_portfolio_tools(app: FastMCP, session_registry: SessionRegistry) -> None:
    """Register the Phase 5 portfolio & analytics tools on ``app``.

    The tool callables close over the shared in-memory session registry and read the
    bound broker's state. This is the only place these tools are registered;
    ``build_app`` invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        session_registry: The shared in-memory execution-session registry.
    """

    def _empty_portfolio() -> Portfolio:
        """A flat portfolio for a session with no broker yet (no trades placed)."""
        return Portfolio(equity=0.0, cash=0.0, position_value=0.0)

    @app.tool(
        name="get_portfolio",
        title="Get session portfolio",
        description=(
            "Return the mark-to-market portfolio snapshot for a session: equity (cash + "
            "position value), cash, position value, the open position (or null when flat), "
            "the latest mark price, and the last bar time. Reads the in-memory broker only "
            "-- no network."
        ),
        structured_output=True,
    )
    def get_portfolio(session_id: str) -> Portfolio:
        """Return the :class:`Portfolio` snapshot for ``session_id`` (flat if no broker)."""
        broker = session_registry.broker_for(session_id)
        if broker is None:
            # Validate the id exists (raises a redacted error otherwise).
            session_registry.get(session_id)
            return _empty_portfolio()
        return broker.portfolio()

    @app.tool(
        name="get_pnl",
        title="Get session PnL",
        description=(
            "Return the realized/unrealized profit decomposition for a session: realized, "
            "unrealized, fees paid, funding paid, total, and return percent of initial cash. "
            "Reads the in-memory broker only -- no network."
        ),
        structured_output=True,
    )
    def get_pnl(session_id: str) -> PnLBreakdown:
        """Return the :class:`PnLBreakdown` for ``session_id`` (zeros if no broker)."""
        broker = session_registry.broker_for(session_id)
        if broker is None:
            session_registry.get(session_id)
            return PnLBreakdown(
                realized=0.0,
                unrealized=0.0,
                fees_paid=0.0,
                funding_paid=0.0,
                total=0.0,
                return_pct=0.0,
            )
        return broker.pnl()

    @app.tool(
        name="get_trade_history",
        title="Get session trade history",
        description=(
            "Return the closed round-trip trades for a session (entry/exit price + time, "
            "size, net pnl, fees, funding, bars held, exit reason), in close order, plus a "
            "count. Reads the in-memory broker only -- no network."
        ),
        structured_output=True,
    )
    def get_trade_history(session_id: str) -> TradeHistoryResult:
        """Return the closed-trade history for ``session_id``."""
        broker = session_registry.broker_for(session_id)
        if broker is None:
            session_registry.get(session_id)
            return TradeHistoryResult(session_id=session_id, trades=[], count=0)
        trades = broker.trade_history()
        return TradeHistoryResult(session_id=session_id, trades=trades, count=len(trades))
