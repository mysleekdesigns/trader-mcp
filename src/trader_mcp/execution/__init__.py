"""Paper/testnet execution runtime (owned by ``backtest-engine-engineer``, Phase 5).

INVARIANT 1 -- ONE interpreter for backtest AND live. This package is the LIVE half
of that invariant. :class:`StrategyRuntime` drives every strategy decision through
:meth:`trader_mcp.engine.SpecInterpreter.signal_at` over a trailing rolling buffer
-- the exact per-bar method the backtest's vectorized :meth:`SpecInterpreter.
signals` reduces to. It reimplements NO signal logic. Feeding the same bars to this
runtime bar-by-bar and to :func:`trader_mcp.engine.run_backtest` over the whole
window yields identical trades/fills/equity. Any divergence is a parity bug (QA's
parity suite asserts it; see ``tests/execution/test_runtime_parity.py``).

INVARIANT 2 -- safe-by-default / specs-are-data. :class:`PaperBroker` is a PURE
in-memory simulation: no network, no orders, no credentials -- the online, stateful
analogue of :class:`trader_mcp.engine.SimulatedBroker`, advancing one bar / one
order at a time with the SAME fill math (next-bar-open fills, taker fee, the spec's
sizing modes, adversarial slippage, perp funding). ``SessionMode`` is ``paper`` |
``testnet`` only -- real-money ``live`` trading is Phase 6 and arms separately
through the safety gates. Every :class:`OrderRecord` from the paper broker carries
``simulated=True``. The runtime depends on a structural :class:`BarFeed` /
:class:`BrokerProtocol` and never imports ``exchanges/``, so the whole package is
fully offline-testable.

This package registers **no** MCP tools/resources/prompts and does not import the
MCP SDK (that is the server lane). The server engineer wraps the public surface
below; the safety engineer routes intents through its gates.

Public surface (the integration contract):
    Runtime + broker + sessions:
        * :class:`StrategyRuntime` (``on_bar``/``run``/``stop``/``finalize``)
        * :class:`PaperBroker` (``submit``/``on_bar``/``cancel``/``open_orders``/
          ``positions``/``balance``/``portfolio``/``pnl``/``trade_history``/
          ``force_close``)
        * :class:`SessionRegistry` (``create``/``deploy``/``start``/``stop``/``get``/
          ``list``/``status``/``runtime_for``/``broker_for``)
        * Protocols: :class:`BarFeed`, :class:`BrokerProtocol`

    Typed models:
        * :class:`ExecutionConfig`, :class:`OrderIntent`, :class:`OrderRecord`,
          :class:`PaperPosition`, :class:`PaperBalance`, :class:`Portfolio`,
          :class:`PnLBreakdown`, :class:`TradeRecord`, :class:`SessionInfo`,
          :class:`SessionStatus`
        * Literals: :data:`SessionMode`, :data:`SessionState`, :data:`OrderSide`,
          :data:`OrderType`, :data:`OrderStatus`, :data:`IntentReason`,
          :data:`PositionSide`
"""

from __future__ import annotations

from trader_mcp.execution.models import (
    ExecutionConfig,
    IntentReason,
    OrderIntent,
    OrderRecord,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperBalance,
    PaperPosition,
    PnLBreakdown,
    Portfolio,
    PositionSide,
    SessionInfo,
    SessionMode,
    SessionState,
    SessionStatus,
    TradeRecord,
)
from trader_mcp.execution.paper_broker import PaperBroker
from trader_mcp.execution.runtime import BarFeed, BrokerProtocol, StrategyRuntime
from trader_mcp.execution.session import SessionRegistry

__all__ = [
    "BarFeed",
    "BrokerProtocol",
    "ExecutionConfig",
    "IntentReason",
    "OrderIntent",
    "OrderRecord",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PaperBalance",
    "PaperBroker",
    "PaperPosition",
    "PnLBreakdown",
    "Portfolio",
    "PositionSide",
    "SessionInfo",
    "SessionMode",
    "SessionRegistry",
    "SessionState",
    "SessionStatus",
    "StrategyRuntime",
    "TradeRecord",
]
