"""Process-wide in-memory registry of paper/testnet execution sessions (Phase 5).

A :class:`SessionRegistry` is the lifecycle manager the MCP-server engineer wraps
for ``start_paper_session`` / ``deploy_strategy`` / ``get_session_status`` /
``stop_session``. It is *instantiable* (no module-level singleton) so tests get
isolated registries -- exactly like :class:`trader_mcp.engine.BacktestStore`. State
lives entirely in memory: a session bundles a :class:`SessionInfo` identity, an
optional :class:`~trader_mcp.execution.runtime.StrategyRuntime` +
:class:`~trader_mcp.execution.paper_broker.PaperBroker`, and the async run-task
handle.

SAFE-BY-DEFAULT: a session is ``paper`` (pure simulation) or ``testnet``; ``live``
is not a valid mode here (Phase 6). Deploying a strategy binds a PaperBroker (no
network, no credentials) by default; a testnet broker is injected from the server
lane via the runtime's :class:`~trader_mcp.execution.runtime.BrokerProtocol`, never
constructed here.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from trader_mcp.execution.models import SessionInfo, SessionStatus
from trader_mcp.execution.paper_broker import PaperBroker
from trader_mcp.execution.runtime import StrategyRuntime

if TYPE_CHECKING:
    from trader_mcp.execution.models import ExecutionConfig, SessionMode
    from trader_mcp.execution.paper_broker import PaperBroker as _PaperBroker
    from trader_mcp.execution.runtime import BarFeed
    from trader_mcp.strategy import StrategySpec


class _Session:
    """Internal mutable bundle for one session (info + runtime + run task)."""

    def __init__(self, info: SessionInfo) -> None:
        self.info = info
        self.runtime: StrategyRuntime | None = None
        self.broker: _PaperBroker | None = None
        self.task: asyncio.Task[None] | None = None
        self.state: str = "stopped"
        self.error: str | None = None


class SessionRegistry:
    """In-memory registry of execution sessions (instantiable; no global singleton).

    Construct one per server process (or per test). It mints session ids, tracks
    each session's runtime/broker, and manages the async run-task lifecycle.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._seq = itertools.count(1)

    # -- creation -------------------------------------------------------------
    def create(
        self,
        *,
        mode: SessionMode,
        exchange: str,
        symbol: str,
        strategy_name: str | None = None,
    ) -> SessionInfo:
        """Create a new (idle) session and return its identity.

        The session is created in the ``stopped`` state with no runtime bound; call
        :meth:`deploy` to attach a strategy and :meth:`start` to run it.
        """
        session_id = f"sess-{next(self._seq)}"
        info = SessionInfo(
            session_id=session_id,
            mode=mode,
            exchange=exchange,
            symbol=symbol,
            strategy_name=strategy_name,
            created=datetime.now(tz=UTC),
        )
        self._sessions[session_id] = _Session(info)
        return info

    # -- strategy binding -----------------------------------------------------
    def deploy(
        self,
        session_id: str,
        spec: StrategySpec,
        *,
        config: ExecutionConfig | None = None,
        broker: _PaperBroker | None = None,
        buffer_size: int | None = None,
    ) -> SessionInfo:
        """Bind a :class:`StrategyRuntime` (+ broker) to ``session_id``.

        Defaults to a pure-simulation :class:`PaperBroker`; a caller (server lane)
        may inject a testnet broker implementing the runtime's
        :class:`~trader_mcp.execution.runtime.BrokerProtocol`. Returns the updated
        :class:`SessionInfo` (now carrying the strategy name).
        """
        session = self._require(session_id)
        used_broker = broker if broker is not None else PaperBroker(spec, config)
        session.broker = used_broker  # type: ignore[assignment]
        session.runtime = StrategyRuntime(spec, used_broker, buffer_size=buffer_size)
        session.info = session.info.model_copy(update={"strategy_name": spec.name})
        return session.info

    # -- run lifecycle --------------------------------------------------------
    def start(self, session_id: str, feed: BarFeed) -> asyncio.Task[None]:
        """Start the async run loop for a deployed session over ``feed``.

        Spawns a cancellable :class:`asyncio.Task` running the runtime's loop and
        records it on the session. Raises if no strategy is deployed or a task is
        already running. Must be called from within a running event loop.
        """
        session = self._require(session_id)
        if session.runtime is None:
            raise RuntimeError(f"session {session_id!r} has no deployed strategy")
        if session.task is not None and not session.task.done():
            raise RuntimeError(f"session {session_id!r} is already running")

        runtime = session.runtime

        async def _runner() -> None:
            try:
                session.state = "running"
                await runtime.run(feed)
                session.state = "stopped"
            except asyncio.CancelledError:
                session.state = "stopped"
                raise
            except Exception as exc:
                session.state = "error"
                session.error = str(exc)

        session.task = asyncio.ensure_future(_runner())
        return session.task

    async def stop(self, session_id: str) -> SessionStatus:
        """Cancel a session's run loop (if any), force-close inventory, and report.

        Idempotent: stopping an idle/finished session is a no-op beyond the
        force-close accounting. Returns the final :class:`SessionStatus`.
        """
        session = self._require(session_id)
        if session.runtime is not None:
            session.runtime.stop()
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if session.runtime is not None:
            session.runtime.finalize()
        if session.state != "error":
            session.state = "stopped"
        return self.status(session_id)

    # -- queries --------------------------------------------------------------
    def get(self, session_id: str) -> SessionInfo:
        """Return the static :class:`SessionInfo` for ``session_id``."""
        return self._require(session_id).info

    def list(self) -> list[SessionInfo]:
        """Return every session's identity, in creation order."""
        return [s.info for s in self._sessions.values()]

    def status(self, session_id: str) -> SessionStatus:
        """Return the live operational snapshot for ``session_id``."""
        session = self._require(session_id)
        info = session.info
        runtime = session.runtime
        broker = session.broker
        bars = runtime.bars_processed if runtime is not None else 0
        last = runtime.last_bar_time if runtime is not None else None
        orders = len(broker.order_history()) if broker is not None else 0
        trades = len(broker.trade_history()) if broker is not None else 0
        open_pos = bool(broker.positions()) if broker is not None else False
        return SessionStatus(
            session_id=info.session_id,
            mode=info.mode,
            exchange=info.exchange,
            symbol=info.symbol,
            strategy_name=info.strategy_name,
            state=session.state,  # type: ignore[arg-type]
            created=info.created,
            bars_processed=bars,
            last_bar_time=last,
            orders_submitted=orders,
            trades_closed=trades,
            open_position=open_pos,
            error=session.error,
        )

    def runtime_for(self, session_id: str) -> StrategyRuntime | None:
        """Return the bound runtime (or ``None`` if undeployed) -- for status/portfolio."""
        return self._require(session_id).runtime

    def broker_for(self, session_id: str) -> _PaperBroker | None:
        """Return the bound broker (or ``None``) -- for portfolio/pnl/trade-history tools."""
        return self._require(session_id).broker

    # -- internals ------------------------------------------------------------
    def _require(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session id {session_id!r}")
        return session
