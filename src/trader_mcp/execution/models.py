"""Typed Pydantic v2 models for the paper/testnet execution runtime (PRD §6 Phase 5).

These are the integration contract the MCP-server engineer wraps as
paper/testnet tool results and the safety engineer routes intents through. They
mirror :mod:`trader_mcp.engine.models`'s house style: small, frozen, and
``extra="forbid"`` (so a construction mistake fails loudly), with timezone-aware
UTC :class:`datetime` timestamps and plain floats in quote/base currency.

SAFE-BY-DEFAULT: these models describe a *paper* (pure in-memory simulation) or a
*testnet* session. ``live`` is intentionally absent from :data:`SessionMode` -- it
is Phase 6 and routes through the armed, gated execution plumbing, never from
here. An :class:`OrderRecord` carries ``simulated=True`` for every paper fill so a
downstream consumer can never confuse a simulated fill for a real one.

PARITY: the fill model behind these records is identical to
:class:`trader_mcp.engine.broker.SimulatedBroker` (next-bar-open fills, taker-fee,
the spec's sizing modes, adversarial slippage, perp funding). A :class:`TradeRecord`
is the online-runtime analogue of :class:`trader_mcp.engine.models.SimulatedTrade`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Which runtime an execution session drives. ``paper`` is a pure in-memory
#: simulation (no network, no credentials); ``testnet`` is a sandbox-exchange
#: session (Phase 5 plumbing; still routed through the safety gates). ``live`` is
#: deliberately NOT here -- real-money trading is Phase 6 and arms separately.
SessionMode = Literal["paper", "testnet"]

#: Lifecycle state of a running session. ``running`` = the async bar loop is live;
#: ``stopped`` = cleanly cancelled; ``error`` = the loop raised and was halted.
SessionState = Literal["running", "stopped", "error"]

#: Order side, matching CCXT's unified schema and :data:`trader_mcp.exchanges.
#: models.OrderSide`.
OrderSide = Literal["buy", "sell"]

#: Order type the v1 runtime emits. ``market`` for entries/exits/DCA; ``limit``
#: for grid levels (the resting-buy convention). Richer types are out of scope.
OrderType = Literal["market", "limit"]

#: Lifecycle status of a placed/simulated order.
#:
#: ``open`` = acknowledged, nothing filled yet (a pending next-bar intent);
#: ``partially_filled`` = some base filled, a remainder still rests; ``filled`` =
#: fully filled (``remaining == 0``); ``canceled`` / ``rejected`` are terminal.
#: ``partially_filled`` exists for the LIVE path: a real exchange can fill an order
#: in pieces, so an :class:`OrderRecord` aggregates those pieces into a cumulative
#: ``filled``/``average`` with the resting ``remaining``. The paper/backtest
#: simulation fills atomically (a single, complete fill), so it only ever produces
#: ``open`` -> ``filled`` -- partial fills therefore CANNOT alter the simulated net
#: position the interpreter expects, and backtest<->live parity is preserved.
OrderStatus = Literal["filled", "partially_filled", "open", "canceled", "rejected"]

#: Why the runtime emitted an intent (for an auditable, human-readable trail).
#: ``signal`` = a rule entry/exit; ``grid`` = a grid-level buy; ``dca`` = a
#: scheduled DCA purchase; ``stop_loss``/``take_profit`` = an intrabar risk exit;
#: ``end_of_session`` = a force-close on stop; ``manual`` = a tool-driven order.
IntentReason = Literal[
    "signal",
    "grid",
    "dca",
    "stop_loss",
    "take_profit",
    "end_of_session",
    "manual",
]

#: Position side for an open paper position (mirrors the engine's ``TradeSide``).
PositionSide = Literal["long", "short"]


class _ExecModel(BaseModel):
    """Base for execution result models: frozen and strict on declared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionConfig(_ExecModel):
    """Fill-realism + reproducibility knobs for one paper session.

    A deliberate sibling of :class:`trader_mcp.engine.models.BacktestConfig` with
    the same field names and defaults so a session and a backtest of the same spec
    produce identical fills (the parity requirement). It is defined here rather than
    reused directly only so the execution package owns its own online knobs without
    importing engine internals beyond the shared math; the values map 1:1.

    * ``initial_cash`` -- starting equity in quote currency.
    * ``slippage_pct`` -- symmetric adversarial slippage fraction (buys fill
      higher, sells lower). ``0`` reproduces a frictionless fill.
    * ``seed`` -- recorded for reproducibility; the fill path has no randomness.
    * ``funding_enabled`` / ``funding_rate`` / ``funding_interval_hours`` -- perp
      funding accrual (only applies to swap symbols), a flat assumed rate because
      the runtime carries no historical funding stream.
    """

    initial_cash: float = Field(default=10_000.0, gt=0)
    slippage_pct: float = Field(default=0.0, ge=0, le=100)
    seed: int = Field(default=0, ge=0)
    funding_enabled: bool = True
    funding_rate: float = Field(default=0.0001, ge=-1, le=1)
    funding_interval_hours: float = Field(default=8.0, gt=0)


class OrderIntent(_ExecModel):
    """What the runtime (or a manual tool call) wants done -- a request, not a fill.

    The runtime translates a :class:`trader_mcp.engine.interpreter.BarSignal` into
    one or more of these and submits them to the broker; the safety layer can
    inspect/gate an intent before it is routed. ``amount`` is in base currency.
    ``price`` is required for a ``limit`` order and ignored for ``market``.
    ``reduce_only`` marks an exit that may only shrink/close a position.
    """

    symbol: str = Field(min_length=1)
    side: OrderSide
    type: OrderType = "market"
    amount: float = Field(gt=0)
    price: float | None = Field(default=None, gt=0)
    client_order_id: str = Field(min_length=1)
    reduce_only: bool = False
    reason: IntentReason = "signal"


class FillEvent(_ExecModel):
    """One (possibly partial) fill of an order -- the LIVE path's atom of execution.

    A real exchange can fill a single order in several pieces; each arriving fill is
    one of these. The paper/backtest simulation fills atomically, so it produces
    exactly one :class:`FillEvent` per order whose ``qty`` equals the order amount --
    a degenerate (complete) partial fill. Aggregating a list of these with
    :func:`aggregate_fills` yields the cumulative ``filled``/``average``/``remaining``
    an :class:`OrderRecord` reports, with over-fill made impossible.

    ``qty`` is the base amount executed by THIS fill (always positive); ``price`` is
    that fill's execution price; ``fee`` is the quote-currency fee for this fill.
    """

    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    fee: float = Field(default=0.0, ge=0)
    timestamp: datetime


class OrderRecord(_ExecModel):
    """The result of submitting an :class:`OrderIntent` to the broker.

    For a paper session every record is a fill (or a rejection) computed in-memory;
    ``simulated`` is always ``True`` for the :class:`~trader_mcp.execution.
    paper_broker.PaperBroker`. ``average`` is the (slippage-adjusted) volume-weighted
    average fill price over ALL fills so far; ``fee`` is the cumulative taker fee paid
    in quote currency; ``filled`` is the cumulative filled base amount and
    ``remaining`` is what still rests (``amount - filled``, never negative).

    PARTIAL FILLS: ``filled``/``average``/``remaining``/``status`` are the aggregate
    of one or more :class:`FillEvent` s (see :func:`aggregate_fills`). The simulation
    only ever produces a single complete fill (``status`` goes ``open`` -> ``filled``,
    ``remaining == 0``), so partial fills are purely a LIVE-path concept and never
    perturb the simulated net position parity depends on. ``remaining > 0`` with
    ``filled > 0`` is reported as ``partially_filled``.
    """

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    status: OrderStatus
    amount: float
    filled: float
    remaining: float = 0.0
    average: float | None
    fee: float
    timestamp: datetime
    reason: IntentReason
    simulated: bool = True


def aggregate_fills(
    *,
    amount: float,
    fills: list[FillEvent],
) -> tuple[float, float, float | None, float, OrderStatus]:
    """Reduce ``fills`` into cumulative ``(filled, remaining, average, fee, status)``.

    The single accounting path shared by the paper broker (one complete fill) and a
    future live broker (many partial fills from the exchange). It guarantees:

    * ``filled`` is the sum of the fills' ``qty`` and ``average`` is their
      volume-weighted price -- so cumulative partials roll up to the right average
      price and position size;
    * **over-fill is impossible**: cumulative ``filled`` is clamped to ``amount`` (a
      misbehaving feed reporting more than the order size cannot inflate the
      position), and ``remaining = max(amount - filled, 0)``. Note ``average`` is
      still the VWAP of the *raw* (unclamped) executed quantities -- it reflects the
      prices actually paid, while only the position size is clamped;
    * ``status`` is ``open`` (nothing filled), ``partially_filled`` (some filled, a
      remainder rests), or ``filled`` (``remaining`` rounds to zero).

    Parity note: with a single fill whose ``qty == amount`` (the simulation's case)
    this returns ``(amount, 0, fill_price, fill_fee, "filled")`` -- exactly the
    atomic fill the backtest broker books, so routing simulated fills through here
    changes no number.
    """
    if not fills:
        return 0.0, amount, None, 0.0, "open"
    raw_filled = sum(f.qty for f in fills)
    filled = min(raw_filled, amount)
    remaining = amount - filled
    if remaining < 0.0:
        remaining = 0.0
    notional = sum(f.qty * f.price for f in fills)
    average = notional / raw_filled if raw_filled > 0 else None
    fee = sum(f.fee for f in fills)
    status: OrderStatus
    if remaining <= 1e-12:
        status = "filled"
    elif filled > 0.0:
        status = "partially_filled"
    else:
        status = "open"
    return filled, remaining, average, fee, status


class PaperPosition(_ExecModel):
    """The single open position in a paper session (v1 is single-position).

    ``size`` is base units (always positive); ``side`` says which way. ``mark_price``
    is the latest bar close; ``unrealized_pnl`` is marked to it net of entry fee and
    accrued funding; ``realized_pnl`` is the lifetime realized pnl carried for
    context (the session's, not this position's).
    """

    symbol: str
    side: PositionSide
    size: float
    entry_price: float
    entry_time: datetime
    mark_price: float
    unrealized_pnl: float
    realized_pnl: float
    stop_price: float | None = None
    take_price: float | None = None
    funding_paid: float = 0.0
    bars_held: int = 0


class PaperBalance(_ExecModel):
    """A paper account's quote-currency cash plus base-currency holdings.

    The simulation tracks equity as cash + position value (it does not run a
    separate margin ledger, matching :class:`~trader_mcp.engine.broker.
    SimulatedBroker`). ``cash`` is realized quote; ``holdings`` is per-base-currency
    net size (signed: negative for a short).
    """

    cash: float
    holdings: dict[str, float] = Field(default_factory=dict)


class Portfolio(_ExecModel):
    """Mark-to-market snapshot of a paper session at the latest bar.

    ``equity`` == ``cash`` + ``position_value`` (the unrealized side of the open
    position is folded into equity exactly as the engine's ``_equity`` does).
    ``position`` is ``None`` when flat.
    """

    equity: float
    cash: float
    position_value: float
    position: PaperPosition | None = None
    mark_price: float | None = None
    timestamp: datetime | None = None


class PnLBreakdown(_ExecModel):
    """Realized/unrealized profit decomposition for a paper session.

    All values are quote currency except ``return_pct`` (percent of initial cash).
    ``total`` == ``realized`` + ``unrealized``. ``fees_paid`` and ``funding_paid``
    are lifetime sums already reflected in the realized/unrealized figures.
    """

    realized: float
    unrealized: float
    fees_paid: float
    funding_paid: float
    total: float
    return_pct: float


class TradeRecord(_ExecModel):
    """A closed round-trip in a paper session -- the online analogue of
    :class:`trader_mcp.engine.models.SimulatedTrade`.

    Same fields and semantics so a session's trade history is directly comparable
    to a backtest's. ``pnl`` is net of all fees and funding; ``pnl_pct`` is that
    pnl over the notional entry value.
    """

    side: PositionSide
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    fees_paid: float
    funding_paid: float
    bars_held: int
    exit_reason: str


class SessionInfo(_ExecModel):
    """Static identity + creation metadata for one execution session.

    ``strategy_name`` is ``None`` for a bare (manual-trading) session and set once a
    strategy is deployed onto it. ``session_id`` is a stable, unique id assigned at
    creation.
    """

    session_id: str
    mode: SessionMode
    exchange: str
    symbol: str
    strategy_name: str | None
    created: datetime


class SessionStatus(_ExecModel):
    """Live operational snapshot of a session (the ``get_session_status`` result).

    Combines the static :class:`SessionInfo` identity with the mutable lifecycle
    counters so a single tool call answers "what is this session doing now". ``state``
    is the run-loop lifecycle; ``error`` carries the failure message when
    ``state == "error"``.
    """

    session_id: str
    mode: SessionMode
    exchange: str
    symbol: str
    strategy_name: str | None
    state: SessionState
    created: datetime
    bars_processed: int
    last_bar_time: datetime | None
    orders_submitted: int
    trades_closed: int
    open_position: bool
    error: str | None = None
