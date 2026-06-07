"""Backtest & live-execution engine (owned by ``backtest-engine-engineer``).

INVARIANT 1 -- ONE interpreter for backtest AND live. :class:`SpecInterpreter` is
the single event-driven signal core. Its vectorized :meth:`SpecInterpreter.signals`
(whole cached window) and its per-bar :meth:`SpecInterpreter.signal_at` (trailing
window -- the live/streaming path Phase 5 plugs into) share the SAME rule
evaluation, so the same bars yield identical signals vectorized vs bar-by-bar.
Never fork strategy logic between backtest and live -- any divergence is a parity
bug. QA's parity suite targets these two methods directly.

INVARIANT 2 -- safe-by-default / specs-are-data. A :class:`StrategySpec` is read,
never written. :class:`SimulatedBroker` is a pure in-memory simulation: no real
orders, no credentials, no network. All rule evaluation flows through the existing
safe evaluator (no ``eval``/``exec``).

Fill convention (no look-ahead): a signal derived from the CLOSE of bar ``t`` is
executed at the OPEN of bar ``t+1`` (backtesting.py's default next-bar-open), with
the sole exception of intrabar stop-loss/take-profit, which fill at their price
level on bar ``t``. This is what QA configures backtesting.py to match.

This package registers **no** MCP tools/resources/prompts (that is the server
lane) and does not import the MCP SDK. quantstats is imported lazily, only inside
:func:`generate_tearsheet`; vectorbt is an optional, guarded accelerator inside
:func:`optimize_strategy` (the native+Optuna path is always the source of truth).

Public surface (the integration contract -- the MCP-server & QA engineers code
against exactly these):

    Run a backtest:
        * :func:`run_backtest` ``(spec, bars_or_result, *, config=None) -> BacktestReport``
        * :func:`run_backtest_from_store`
          ``(spec, *, store=None, since=None, until=None, config=None) -> BacktestReport``

    Optimize / walk-forward:
        * :func:`optimize_strategy`
          ``(spec, bars_or_result, params, *, objective="sharpe", n_trials=50,
            walk_forward_folds=0, config=None, use_vectorbt=False) -> OptimizeResult``

    Tear sheet (lazy quantstats):
        * :func:`generate_tearsheet`
          ``(report, *, data_dir=None, title=None) -> TearsheetResult``

    Persistence / comparison:
        * :class:`BacktestStore` (``save``/``load``/``delete``/``exists``/
          ``list_ids``/``path_for``)
        * :func:`compare_reports` ``(reports, *, objective="sharpe") -> BacktestComparison``

    Native metrics:
        * :func:`compute_metrics`
          ``(equity_curve, trades, *, initial_cash, timeframe, total_bars) ->
            BacktestMetrics``
        * :func:`periods_per_year` ``(timeframe) -> float``

    The interpreter & broker (for parity tests / Phase 5):
        * :class:`SpecInterpreter` (``prepare``/``signals``/``signal_at``,
          ``LIVE_WINDOW``), :class:`BarSignal`
        * :class:`SimulatedBroker`

    Typed models:
        * :class:`BacktestConfig`, :class:`BacktestReport`, :class:`BacktestMetrics`,
          :class:`SimulatedTrade`, :class:`EquityPoint`, :data:`ExitReason`,
          :data:`TradeSide`
        * :class:`ParamSpec`, :class:`OptimizeTrial`, :class:`WalkForwardFold`,
          :class:`OptimizeResult`, :class:`TearsheetResult`
        * :class:`BacktestComparison`, :class:`BacktestComparisonRow`
"""

from __future__ import annotations

from trader_mcp.engine.backtest import run_backtest, run_backtest_from_store
from trader_mcp.engine.broker import SimulatedBroker
from trader_mcp.engine.interpreter import BarSignal, SpecInterpreter
from trader_mcp.engine.metrics import compute_metrics, periods_per_year
from trader_mcp.engine.models import (
    BacktestComparison,
    BacktestComparisonRow,
    BacktestConfig,
    BacktestMetrics,
    BacktestReport,
    EquityPoint,
    ExitReason,
    OptimizeResult,
    OptimizeTrial,
    ParamSpec,
    SimulatedTrade,
    TearsheetResult,
    TradeSide,
    WalkForwardFold,
)
from trader_mcp.engine.optimize import optimize_strategy
from trader_mcp.engine.store import BacktestStore, compare_reports
from trader_mcp.engine.tearsheet import generate_tearsheet

__all__ = [
    "BacktestComparison",
    "BacktestComparisonRow",
    "BacktestConfig",
    "BacktestMetrics",
    "BacktestReport",
    "BacktestStore",
    "BarSignal",
    "EquityPoint",
    "ExitReason",
    "OptimizeResult",
    "OptimizeTrial",
    "ParamSpec",
    "SimulatedBroker",
    "SimulatedTrade",
    "SpecInterpreter",
    "TearsheetResult",
    "TradeSide",
    "WalkForwardFold",
    "compare_reports",
    "compute_metrics",
    "generate_tearsheet",
    "optimize_strategy",
    "periods_per_year",
    "run_backtest",
    "run_backtest_from_store",
]
