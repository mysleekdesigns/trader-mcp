"""Phase 5 paper/testnet execution MCP tools (PRD §5.2 "paper/testnet execution").

Wraps the execution runtime (:mod:`trader_mcp.execution`) as a typed,
structured-output MCP tool surface: open a session, place/cancel orders, inspect
open orders / positions / balance, deploy a saved strategy onto a session and run
it, stop it, and report its status.

SAFE-BY-DEFAULT (INVARIANT 2). Every order-placing path here -- ``place_order`` and
``deploy_strategy`` -- consults the single safety chokepoint
:func:`trader_mcp.safety.evaluate_order` BEFORE any order is constructed and honors
its verdict:

    * ``simulate`` -> the order is recorded against the session's in-memory
      :class:`~trader_mcp.execution.PaperBroker` (no network, no real order);
    * ``route``    -> the order is sent to the real exchange *sandbox* (testnet,
      fake money) via :meth:`ExchangeAdapter.create_order` with an idempotent
      client order id;
    * ``deny``     -> a redacted :class:`~trader_mcp.errors.SafetyError` is raised.

The process-wide dry-run default (on unless ``TRADER_MCP_DRY_RUN`` disables it)
downgrades any otherwise-routable testnet decision to ``simulate``, so nothing
reaches a real venue without an explicit opt-in. ``live`` mode is not even a valid
session mode in Phase 5; the gate denies it unconditionally.

ONE INTERPRETER (INVARIANT 1). ``deploy_strategy`` drives the strategy through the
execution :class:`~trader_mcp.execution.StrategyRuntime`, which makes every decision
through the SAME :class:`~trader_mcp.engine.SpecInterpreter` the backtest uses. This
module wires the run loop; it never reimplements signal logic.

Typed I/O: every tool takes Pydantic-validated arguments and returns an execution
Pydantic v2 model (or a thin result wrapper defined here for the list/scalar
returns) with ``structured_output=True`` so FastMCP emits an ``outputSchema``.

Feed for ``deploy_strategy``: a ``testnet`` deploy streams live bars from
:meth:`ExchangeAdapter.watch_ohlcv`. A ``paper`` deploy has no live socket, so it
replays the session's cached OHLCV history from the local store through a small
in-process async feed -- exercising the exact same runtime/interpreter/broker path
a live feed would, fully offline. If no bars are cached the run loop simply ends
immediately (the session stays valid and can be re-deployed after ``sync_history``).

The MCP SDK stays isolated: registration goes through the ``FastMCP`` instance
re-exported from :mod:`trader_mcp.server._sdk`; this module never ``import mcp``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from trader_mcp.config import ExchangeId, KeyScope
from trader_mcp.data import DatasetKey, OHLCVStore
from trader_mcp.exchanges import ExchangeManager, Order
from trader_mcp.execution import (
    OrderIntent,
    OrderRecord,
    PaperBalance,
    PaperBroker,
    PaperPosition,
    SessionInfo,
    SessionRegistry,
    SessionStatus,
)
from trader_mcp.safety import (
    IdempotencyRegistry,
    evaluate_order,
    make_client_order_id,
)
from trader_mcp.safety import (
    SessionMode as GateSessionMode,
)
from trader_mcp.server._sdk import FastMCP
from trader_mcp.strategy import StrategyStore

if TYPE_CHECKING:
    from trader_mcp.exchanges.models import OHLCVBar

#: Session modes a client may open. Mirrors :data:`trader_mcp.execution.SessionMode`
#: (``live`` is intentionally absent -- Phase 6).
ServerSessionMode = Literal["paper", "testnet"]

#: Order side / type literals for the manual ``place_order`` tool (mirror the
#: execution-layer literals so the inputSchema documents the exact accepted set).
ServerOrderSide = Literal["buy", "sell"]
ServerOrderType = Literal["market", "limit"]


class _ExecResult(BaseModel):
    """Base for the thin server-side result wrappers (frozen, strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenOrdersResult(_ExecResult):
    """Open (pending) orders for a session plus their count.

    FastMCP structured output requires a top-level object, so the bare list of
    :class:`~trader_mcp.execution.OrderRecord` is wrapped here.
    """

    session_id: str = Field(description="The session these orders belong to.")
    orders: list[OrderRecord] = Field(description="Orders still in the open/pending state.")
    count: int = Field(description="Number of open orders.")


class PositionsResult(_ExecResult):
    """Open positions for a session plus their count (object wrapper for the list)."""

    session_id: str = Field(description="The session these positions belong to.")
    positions: list[PaperPosition] = Field(description="Open positions (empty when flat).")
    count: int = Field(description="Number of open positions.")


class CancelResult(_ExecResult):
    """Outcome of a cancel request (uniform across paper and testnet)."""

    session_id: str = Field(description="The session the order belonged to.")
    order_id: str = Field(description="The id of the order targeted for cancellation.")
    canceled: bool = Field(description="True if the order was found and canceled.")
    detail: str = Field(description="Human-readable, secret-free outcome note.")


def _market_type_for(symbol: str) -> Literal["spot", "swap"]:
    """Derive the gate market type from the symbol (perps carry a ``:`` settle suffix).

    Matches :class:`trader_mcp.execution.PaperBroker`'s ``is_perp`` convention so the
    jurisdiction check sees the same classification the fill model uses.
    """
    return "swap" if ":" in symbol else "spot"


def _order_to_record(order: Order, *, reason: Literal["manual"] = "manual") -> OrderRecord:
    """Convert a routed exchange :class:`Order` into a typed :class:`OrderRecord`.

    Gives ``place_order`` a single, uniform ``outputSchema`` whether the gate
    simulated (paper broker record) or routed (exchange order). ``simulated=False``
    marks a real (testnet) fill so a consumer can never confuse it with a paper one.
    """
    status: Literal["filled", "open", "canceled", "rejected"]
    raw_status = order.status
    if raw_status in ("filled", "open", "canceled", "rejected"):
        status = raw_status  # type: ignore[assignment]
    elif raw_status == "closed":
        status = "filled"
    else:
        status = "open"
    fee_cost = order.fee.cost if order.fee is not None else None
    from datetime import UTC, datetime

    return OrderRecord(
        order_id=order.id or "",
        client_order_id=order.client_order_id or "",
        symbol=order.symbol,
        side=order.side or "buy",
        type=order.type or "market",
        status=status,
        amount=order.amount or 0.0,
        filled=order.filled or 0.0,
        average=order.average,
        fee=fee_cost or 0.0,
        timestamp=order.timestamp or datetime.now(tz=UTC),
        reason=reason,
        simulated=False,
    )


class _ReplayFeed:
    """A minimal in-process async :class:`~trader_mcp.execution.BarFeed`.

    Replays a fixed, already-materialized list of bars (e.g. the session's cached
    OHLCV history) through the runtime, then ends. Used by a ``paper`` deploy which
    has no live socket -- it exercises the exact runtime/interpreter/broker path a
    live feed would, fully offline.
    """

    def __init__(self, bars: list[OHLCVBar]) -> None:
        self._bars = bars

    async def __aiter__(self) -> AsyncIterator[OHLCVBar]:
        for bar in self._bars:
            yield bar


def register_execution_tools(
    app: FastMCP,
    manager: ExchangeManager,
    session_registry: SessionRegistry,
    strategy_store: StrategyStore,
    store: OHLCVStore,
) -> None:
    """Register the Phase 5 paper/testnet execution tools on ``app``.

    The tool callables close over the process-wide exchange manager (testnet
    routing + reads), the in-memory session registry (lifecycle), the saved-strategy
    store (``deploy_strategy``'s spec source), and the local OHLCV cache (the paper
    replay feed). This is the only place these tools are registered; ``build_app``
    invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        manager: The shared exchange-adapter cache (testnet adapters + key scope).
        session_registry: The shared in-memory execution-session registry.
        strategy_store: The shared local strategy store ``deploy_strategy`` loads from.
        store: The shared local OHLCV cache the paper replay feed reads from.
    """

    # Bare paper brokers for MANUAL orders placed on a session that has no strategy
    # deployed (a deployed session uses the strategy's own broker via the registry).
    # Kept in the tool closure so the execution package / registry stays untouched.
    manual_brokers: dict[str, PaperBroker] = {}

    # Idempotency for manual ``place_order`` calls. A per-session monotonic counter
    # feeds ``make_client_order_id(seq=...)`` so each distinct order on the same
    # (session, symbol, side) gets a distinct, deterministic COID. One process-wide
    # registry dedupes retried submissions: a COID we have already accepted returns
    # its prior result instead of placing/simulating a second order.
    order_seq: dict[str, int] = {}
    idempotency = IdempotencyRegistry()
    order_results: dict[str, OrderRecord] = {}

    def _next_seq(session_id: str) -> int:
        """Return the next monotonic order sequence for ``session_id`` (starting at 0)."""
        seq = order_seq.get(session_id, 0)
        order_seq[session_id] = seq + 1
        return seq

    async def _adapter_and_scope(session: SessionInfo) -> tuple[object, KeyScope]:
        """Resolve a testnet adapter and its credential scope for a session.

        Used only for ``testnet`` sessions (the routing path). The adapter carries
        ``key_scope`` derived from the configured credentials; never returns a key.
        """
        adapter = await manager.get(session.exchange, testnet=True)  # type: ignore[arg-type]
        return adapter, adapter.key_scope

    def _paper_broker_for(session_id: str, symbol: str) -> PaperBroker:
        """Return the session's paper broker, preferring a deployed one.

        Falls back to (and lazily creates) a bare manual broker for an undeployed
        session so manual paper orders have somewhere to record.
        """
        deployed = session_registry.broker_for(session_id)
        if deployed is not None:
            return deployed
        broker = manual_brokers.get(session_id)
        if broker is None:
            broker = _bare_paper_broker(symbol)
            manual_brokers[session_id] = broker
        return broker

    def _read_paper_broker(session_id: str) -> PaperBroker | None:
        """Return the session's broker for a read-only query (no lazy creation)."""
        return session_registry.broker_for(session_id) or manual_brokers.get(session_id)

    @app.tool(
        name="start_session",
        title="Start an execution session",
        description=(
            "Open a paper or testnet trading session for an exchange + symbol and return "
            "its identity (session_id, mode, created). 'paper' is a pure in-process "
            "simulation (no network, no credentials); 'testnet' targets the exchange "
            "sandbox (fake money) and is jurisdiction-checked. The session starts idle -- "
            "place orders manually with place_order or deploy a saved strategy with "
            "deploy_strategy. Safe-by-default: nothing routes to a real venue without an "
            "explicit opt-in (the dry-run gate)."
        ),
        structured_output=True,
    )
    def start_session(
        mode: ServerSessionMode,
        exchange: ExchangeId,
        symbol: str,
    ) -> SessionInfo:
        """Create and return a new idle execution session.

        For ``testnet`` the (exchange, market) is jurisdiction-checked up front so a
        client learns immediately if the venue/market is not US-eligible; ``paper``
        needs no eligibility check.
        """
        if mode == "testnet":
            # Fail fast on an ineligible venue/market before a session is minted.
            from trader_mcp.safety import check_jurisdiction

            check_jurisdiction(exchange, _market_type_for(symbol))
        return session_registry.create(mode=mode, exchange=exchange, symbol=symbol)

    @app.tool(
        name="place_order",
        title="Place an order (safe-by-default)",
        description=(
            "Place a single order on a session. THE SAFE PATH: the order is run through "
            "the safety gate first. paper -> always simulated through the in-process paper "
            "broker (no real order); testnet -> routed to the exchange sandbox (fake money) "
            "ONLY when the key is trade-enabled, the venue/market is US-eligible, AND the "
            "global dry-run default is off -- otherwise it is downgraded to simulate. live "
            "is not a valid session mode. A denied order raises a redacted safety error. "
            "Each order gets a distinct, deterministic client order id; pass an explicit "
            "seq to make a retry idempotent (a resubmission with the same seq is deduped "
            "and returns the prior result). Returns the resulting OrderRecord "
            "(simulated=True for paper, False for a routed testnet fill)."
        ),
        structured_output=True,
    )
    async def place_order(
        session_id: str,
        symbol: str,
        side: ServerOrderSide,
        amount: float,
        type: ServerOrderType = "market",  # noqa: A002 -- matches the order-model field name
        price: float | None = None,
        reduce_only: bool = False,
        seq: int | None = None,
    ) -> OrderRecord:
        """Place ``amount`` of ``symbol`` on ``session_id``, gated for safety.

        Resolves the session's mode and (for testnet) credential scope, consults
        :func:`evaluate_order`, and honors the verdict: ``simulate`` records the
        order on the session's paper broker; ``route`` sends it to the testnet
        exchange with an idempotent client order id; ``deny`` raises a redacted
        :class:`~trader_mcp.errors.SafetyError`.

        Idempotency: each order gets a distinct, deterministic client order id
        derived from (session, symbol, side, seq). Omit ``seq`` and the server auto-
        increments a per-session counter so every call is a new, distinct order; pass
        an explicit ``seq`` to make a retry idempotent -- a resubmission with the same
        ``seq`` produces the same COID and is deduped (the prior :class:`OrderRecord`
        is returned, no second order is placed/simulated).
        """
        info = session_registry.get(session_id)
        market_type = _market_type_for(symbol)
        notional = amount * (price if price is not None else 0.0)

        # Resolve key scope: testnet needs a real adapter/credential; paper does not
        # (the gate returns ``simulate`` for paper regardless of scope).
        adapter: object | None = None
        if info.mode == "testnet":
            adapter, key_scope = await _adapter_and_scope(info)
        else:
            key_scope = KeyScope.READ_ONLY

        decision = evaluate_order(
            mode=GateSessionMode(info.mode),
            exchange=info.exchange,
            market_type=market_type,
            key_scope=key_scope,
            amount=amount,
            notional=notional,
        )
        decision.raise_if_denied()

        # Distinct, deterministic COID per order. An explicit ``seq`` makes a retry
        # idempotent; omitting it auto-increments so each call is a new order.
        resolved_seq = _next_seq(session_id) if seq is None else seq
        coid = make_client_order_id(
            session_id=session_id,
            symbol=symbol,
            side=side,
            seq=resolved_seq,
        )
        # Dedupe a retried submission: an already-recorded COID returns its prior
        # result instead of placing/simulating a second order.
        if not idempotency.record(coid):
            return order_results[coid]

        intent = OrderIntent(
            symbol=symbol,
            side=side,
            type=type,
            amount=amount,
            price=price,
            client_order_id=coid,
            reduce_only=reduce_only,
            reason="manual",
        )

        if decision.action == "simulate":
            broker = _paper_broker_for(session_id, symbol)
            record = broker.submit(intent, ref_price=price)
            order_results[coid] = record
            return record

        # decision.action == "route": testnet only (gate guarantees this).
        assert adapter is not None
        # Forward ``reduce_only`` to CCXT (only when set) so a gated exit truly only
        # closes -- the paper path reads ``intent.reduce_only`` directly instead.
        params = {"reduceOnly": True} if reduce_only else None
        order = await adapter.create_order(  # type: ignore[attr-defined]
            symbol,
            type,
            side,
            amount,
            price,
            client_order_id=coid,
            params=params,
        )
        record = _order_to_record(order)
        order_results[coid] = record
        return record

    @app.tool(
        name="cancel_order",
        title="Cancel an order",
        description=(
            "Cancel an open order on a session by order_id. paper -> cancels the pending "
            "order in the in-process broker; testnet -> cancels it on the exchange sandbox "
            "(symbol required for testnet). Returns whether the cancel succeeded."
        ),
        structured_output=True,
    )
    async def cancel_order(
        session_id: str,
        order_id: str,
        symbol: str | None = None,
    ) -> CancelResult:
        """Cancel ``order_id`` on ``session_id`` (paper broker or testnet exchange)."""
        info = session_registry.get(session_id)
        if info.mode == "testnet":
            target_symbol = symbol or info.symbol
            adapter, _scope = await _adapter_and_scope(info)
            order = await adapter.cancel_order(order_id, target_symbol)  # type: ignore[attr-defined]
            canceled = order.status in ("canceled", "closed")
            return CancelResult(
                session_id=session_id,
                order_id=order_id,
                canceled=canceled,
                detail="testnet cancel routed to exchange sandbox",
            )
        broker = _read_paper_broker(session_id)
        if broker is None:
            return CancelResult(
                session_id=session_id,
                order_id=order_id,
                canceled=False,
                detail="no broker on this session (no orders placed yet)",
            )
        ok = broker.cancel(order_id)
        return CancelResult(
            session_id=session_id,
            order_id=order_id,
            canceled=ok,
            detail="paper order canceled" if ok else "no matching open paper order",
        )

    @app.tool(
        name="get_open_orders",
        title="Get open orders",
        description=(
            "List the open (pending) orders for a session. paper -> the in-process broker's "
            "pending orders; testnet -> the open orders on the exchange sandbox. Returns the "
            "orders plus a count."
        ),
        structured_output=True,
    )
    async def get_open_orders(session_id: str) -> OpenOrdersResult:
        """Return the open orders for ``session_id`` (paper broker or testnet exchange)."""
        info = session_registry.get(session_id)
        if info.mode == "testnet":
            adapter, _scope = await _adapter_and_scope(info)
            orders = await adapter.fetch_open_orders(info.symbol)  # type: ignore[attr-defined]
            records = [_order_to_record(o) for o in orders]
            return OpenOrdersResult(session_id=session_id, orders=records, count=len(records))
        broker = _read_paper_broker(session_id)
        orders = broker.open_orders() if broker is not None else []
        return OpenOrdersResult(session_id=session_id, orders=orders, count=len(orders))

    @app.tool(
        name="get_positions",
        title="Get open positions",
        description=(
            "Report the open positions for a session. paper -> the simulated single "
            "position (empty when flat); testnet -> the open positions on the exchange "
            "sandbox (spot venues report none). Returns the positions plus a count."
        ),
        structured_output=True,
    )
    async def get_positions(session_id: str) -> PositionsResult:
        """Return the open positions for ``session_id``."""
        info = session_registry.get(session_id)
        if info.mode == "testnet":
            adapter, _scope = await _adapter_and_scope(info)
            ex_positions = await adapter.fetch_positions([info.symbol])  # type: ignore[attr-defined]
            paper_like = [_position_to_paper(p) for p in ex_positions]
            return PositionsResult(
                session_id=session_id, positions=paper_like, count=len(paper_like)
            )
        broker = _read_paper_broker(session_id)
        positions = broker.positions() if broker is not None else []
        return PositionsResult(session_id=session_id, positions=positions, count=len(positions))

    @app.tool(
        name="get_balance",
        title="Get account balance",
        description=(
            "Report the session account balance. paper -> the simulated quote cash plus net "
            "base holdings; testnet -> the (fake-money) balance on the exchange sandbox, "
            "normalized to the same cash + holdings shape."
        ),
        structured_output=True,
    )
    async def get_balance(session_id: str) -> PaperBalance:
        """Return the session balance (paper account or normalized testnet balance)."""
        info = session_registry.get(session_id)
        if info.mode == "testnet":
            adapter, _scope = await _adapter_and_scope(info)
            bal = await adapter.fetch_balance()  # type: ignore[attr-defined]
            quote = info.symbol.split("/")[-1].split(":")[0]
            cash = 0.0
            holdings: dict[str, float] = {}
            for currency, entry in bal.entries.items():
                total = entry.total or 0.0
                if currency == quote:
                    cash = total
                elif total:
                    holdings[currency] = total
            return PaperBalance(cash=cash, holdings=holdings)
        broker = _read_paper_broker(session_id)
        if broker is None:
            return PaperBalance(cash=0.0, holdings={})
        return broker.balance()

    @app.tool(
        name="deploy_strategy",
        title="Deploy a strategy onto a session",
        description=(
            "Load a saved strategy and run it on a session through the SAME event-driven "
            "interpreter the backtest uses (backtest<->live parity). Safe-by-default: a "
            "paper deploy simulates every order; a testnet deploy still routes orders only "
            "when the gate says route. The feed is the exchange's live OHLCV stream for "
            "testnet, or a replay of the session's cached OHLCV history for paper (so the "
            "runtime is exercised fully offline). Returns the updated SessionInfo (now "
            "carrying the strategy name); poll get_session_status for progress."
        ),
        structured_output=True,
    )
    async def deploy_strategy(session_id: str, strategy_name: str) -> SessionInfo:
        """Deploy ``strategy_name`` onto ``session_id`` and start its run loop.

        Loads the spec, binds a runtime (a fresh paper broker for paper; a testnet
        broker would be injected for routed testnet, which remains gated), and starts
        the async loop over the appropriate feed. Raises a redacted not-found error
        if the strategy is missing.
        """
        info = session_registry.get(session_id)
        spec = strategy_store.load(strategy_name)
        session_registry.deploy(session_id, spec)

        feed: object
        if info.mode == "testnet":
            adapter, _scope = await _adapter_and_scope(info)
            feed = _WatchFeed(adapter, info.symbol, spec.timeframe)  # type: ignore[arg-type]
        else:
            result = store.read_bars(
                DatasetKey(
                    exchange=spec.exchange,
                    symbol=spec.symbol,
                    timeframe=spec.timeframe,
                )
            )
            feed = _ReplayFeed(result.bars)

        session_registry.start(session_id, feed)  # type: ignore[arg-type]
        return session_registry.get(session_id)

    @app.tool(
        name="stop_strategy",
        title="Stop a session's strategy",
        description=(
            "Stop a running session: cancel its run loop, force-close open inventory for "
            "end-of-session accounting, and return the final SessionStatus. Idempotent -- "
            "stopping an idle/finished session is safe."
        ),
        structured_output=True,
    )
    async def stop_strategy(session_id: str) -> SessionStatus:
        """Stop the run loop for ``session_id`` and return its final status."""
        return await session_registry.stop(session_id)

    @app.tool(
        name="get_session_status",
        title="Get session status",
        description=(
            "Return a live operational snapshot of a session: its identity, lifecycle state "
            "(running/stopped/error), bars processed, last bar time, orders submitted, "
            "trades closed, and whether a position is open."
        ),
        structured_output=True,
    )
    def get_session_status(session_id: str) -> SessionStatus:
        """Return the current :class:`SessionStatus` for ``session_id``."""
        return session_registry.status(session_id)


def _bare_paper_broker(symbol: str) -> PaperBroker:
    """Build a minimal :class:`PaperBroker` for manual orders on an undeployed session.

    A paper broker requires a spec for sizing/fees; for a bare manual session we
    synthesize a trivial fixed-base rule spec so manual paper orders have a broker to
    ack against. (A deployed session uses the strategy's own broker instead.)
    """
    from trader_mcp.strategy import EntryRules, StrategySpec

    _base, _, quote = symbol.partition("/")
    spec = StrategySpec(
        name=f"manual-{symbol}",
        symbol=symbol if quote else f"{symbol}/USD",
        strategy_type="rule",
        # A trivially-never-true rule: the manual broker only needs the spec for
        # symbol/fees/sizing -- no runtime drives these rules.
        entry=EntryRules(long="close < 0"),
    )
    return PaperBroker(spec)


def _position_to_paper(position: object) -> PaperPosition:
    """Convert a normalized exchange :class:`Position` into a :class:`PaperPosition`.

    Best-effort mapping so testnet ``get_positions`` shares the paper ``outputSchema``.
    Missing optional CCXT fields default to zero/None.
    """
    from datetime import UTC, datetime

    from trader_mcp.exchanges.models import Position as _Position

    assert isinstance(position, _Position)
    return PaperPosition(
        symbol=position.symbol,
        side=position.side or "long",
        size=position.contracts or 0.0,
        entry_price=position.entry_price or 0.0,
        entry_time=position.timestamp or datetime.now(tz=UTC),
        mark_price=position.mark_price or position.entry_price or 0.0,
        unrealized_pnl=position.unrealized_pnl or 0.0,
        realized_pnl=0.0,
    )


class _WatchFeed:
    """An async :class:`~trader_mcp.execution.BarFeed` over ``adapter.watch_ohlcv``.

    Wraps the CCXT-Pro live OHLCV stream so a testnet deploy feeds real streaming
    bars through the same runtime a paper replay feed drives. The runtime owns
    cancellation; this just forwards the async iterator.
    """

    def __init__(self, adapter: object, symbol: str, timeframe: str) -> None:
        self._adapter = adapter
        self._symbol = symbol
        self._timeframe = timeframe

    def __aiter__(self) -> AsyncIterator[OHLCVBar]:
        return self._adapter.watch_ohlcv(self._symbol, self._timeframe)  # type: ignore[attr-defined,no-any-return]
