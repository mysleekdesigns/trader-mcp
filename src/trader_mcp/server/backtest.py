"""Phase 4 backtest & optimize MCP tools (PRD §5.2 "backtest & optimize", §6 Phase 4).

Wraps the event-driven backtest engine (:mod:`trader_mcp.engine`) as a typed,
structured-output MCP tool surface: run a backtest over cached data, fetch/compare
saved reports, sweep parameters (with optional walk-forward), and render a
quantstats tear sheet.

These tools are SAFE by construction. A backtest is pure simulation over the local
DuckDB+Parquet OHLCV cache: it reads bars, runs the same single event-driven
interpreter the live path uses (INVARIANT 1: never fork strategy logic), and marks
to market in an in-memory :class:`~trader_mcp.engine.SimulatedBroker`. There is no
order, arming, network, or credential surface here, so the safe-by-default
invariant holds trivially -- there is nothing to gate. A strategy spec is *data,
never code*; the interpreter evaluates every rule through the safe-evaluator
whitelist.

Typed I/O: every tool takes Pydantic-validated arguments and returns an engine
Pydantic v2 model (``BacktestReport`` / ``OptimizeResult`` / ``BacktestComparison``
/ ``TearsheetResult``) with ``structured_output=True`` so FastMCP emits an
``outputSchema``. ``optimize_strategy`` takes its ``params`` as a typed
``list[ParamSpec]`` so FastMCP also emits a real ``inputSchema`` documenting each
tunable's dotted path and bounds.

Error model: missing strategies, empty (un-synced) data windows, missing reports,
and unknown objectives surface as redacted :class:`~trader_mcp.errors.ValidationError`
raised by the engine/stores. They propagate to the MCP boundary where FastMCP
surfaces them; the engine never leaks raw input or secrets. A common first-run
error is "sync first": :func:`run_backtest_from_store` raises when no cached bars
exist for the spec's (exchange, symbol, timeframe) -- call ``sync_history`` first.

The MCP SDK stays isolated: registration goes through the ``FastMCP`` instance
re-exported from :mod:`trader_mcp.server._sdk`; this module never ``import mcp``.
"""

from __future__ import annotations

from datetime import datetime

from trader_mcp.data import DatasetKey, OHLCVStore
from trader_mcp.engine import (
    BacktestComparison,
    BacktestConfig,
    BacktestReport,
    BacktestStore,
    OptimizeResult,
    ParamSpec,
    TearsheetResult,
    compare_reports,
    generate_tearsheet,
    optimize_strategy,
    run_backtest_from_store,
)
from trader_mcp.server._sdk import FastMCP
from trader_mcp.strategy import StrategyStore


def _build_config(
    *,
    initial_cash: float | None,
    slippage_pct: float | None,
    seed: int | None,
) -> BacktestConfig:
    """Build a :class:`BacktestConfig` from optional overrides (defaults elsewhere).

    Only the three fill/reproducibility knobs the tools expose are overridable; the
    funding fields keep their documented engine defaults. Each unset override falls
    back to the :class:`BacktestConfig` field default, so omitting all three yields
    the documented cross-validatable baseline (``$10k`` cash, no slippage, seed 0).
    Out-of-range values are rejected by ``BacktestConfig``'s own field validators.
    """
    defaults = BacktestConfig()
    return BacktestConfig(
        initial_cash=defaults.initial_cash if initial_cash is None else initial_cash,
        slippage_pct=defaults.slippage_pct if slippage_pct is None else slippage_pct,
        seed=defaults.seed if seed is None else seed,
    )


def register_backtest_tools(
    app: FastMCP,
    strategy_store: StrategyStore,
    store: OHLCVStore,
    backtest_store: BacktestStore,
) -> None:
    """Register the Phase 4 backtest & optimize tools on ``app``.

    The tool callables close over the three process-wide stores: the saved-strategy
    file store (the spec source), the local OHLCV cache (the bar feed), and the
    backtest-report file store (where runs are persisted and served from). This is
    the only place these tools are registered; ``build_app`` invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        strategy_store: The shared local strategy store specs are loaded from.
        store: The shared local OHLCV cache backtests read their bars from.
        backtest_store: The shared local store backtest reports are saved to/loaded
            from (and served as ``backtest://`` resources).
    """

    @app.tool(
        name="run_backtest",
        title="Run a backtest",
        description=(
            "Backtest a saved strategy over its cached OHLCV data and persist the report. "
            "Loads the named strategy, reads cached bars for its (exchange, symbol, "
            "timeframe) -- sync_history first if none exist -- runs the event-driven "
            "interpreter (the same one the live path uses), and saves the typed report "
            "(addressed by a deterministic report_id; re-running identical inputs "
            "overwrites the same report). Optional since/until window the data; "
            "initial_cash/slippage_pct/seed override the fill model (defaults: $10k, no "
            "slippage, seed 0). Pure simulation -- no orders, no network beyond the local "
            "cache."
        ),
        structured_output=True,
    )
    def run_backtest(
        strategy_name: str,
        since: datetime | None = None,
        until: datetime | None = None,
        initial_cash: float | None = None,
        slippage_pct: float | None = None,
        seed: int | None = None,
    ) -> BacktestReport:
        """Backtest the saved strategy ``strategy_name``, persist, and return the report.

        Raises a redacted not-found ``ValidationError`` if the strategy is missing,
        and a redacted "sync first" ``ValidationError`` if no cached bars exist for
        the spec's (exchange, symbol, timeframe) window.
        """
        spec = strategy_store.load(strategy_name)
        config = _build_config(initial_cash=initial_cash, slippage_pct=slippage_pct, seed=seed)
        report = run_backtest_from_store(spec, store=store, since=since, until=until, config=config)
        backtest_store.save(report)
        return report

    @app.tool(
        name="get_backtest_report",
        title="Get a backtest report",
        description=(
            "Load and return the full saved BacktestReport for report_id (metrics, every "
            "trade, the equity curve, and the run config). Raises a redacted not-found "
            "error if no such report exists. Reads the local store only -- no network."
        ),
        structured_output=True,
    )
    def get_backtest_report(report_id: str) -> BacktestReport:
        """Return the saved :class:`BacktestReport` for ``report_id`` (raises if absent)."""
        return backtest_store.load(report_id)

    @app.tool(
        name="compare_backtests",
        title="Compare backtest reports",
        description=(
            "Load several saved reports by id and return a side-by-side comparison ranked "
            "by an objective metric (default 'sharpe'; e.g. total_return_pct, cagr_pct, "
            "sortino, calmar, win_rate_pct, profit_factor -- maximized -- or "
            "max_drawdown_pct/volatility_pct -- minimized). Returns one headline-metric "
            "row per report plus the winning report_id. No network."
        ),
        structured_output=True,
    )
    def compare_backtests(report_ids: list[str], objective: str = "sharpe") -> BacktestComparison:
        """Compare the saved reports named in ``report_ids``, ranked by ``objective``.

        Each id is loaded via the backtest store (a redacted not-found error is
        raised for a missing id); an unknown ``objective`` is rejected by the
        engine's comparator.
        """
        reports = [backtest_store.load(rid) for rid in report_ids]
        return compare_reports(reports, objective=objective)

    @app.tool(
        name="optimize_strategy",
        title="Optimize a strategy",
        description=(
            "Sweep tunable parameters of a saved strategy over its cached data and return "
            "the best-found set (optionally with a walk-forward breakdown). Each param is "
            "a typed ParamSpec: a dotted spec path (e.g. 'indicators.rsi.length', "
            "'risk.stop_loss_pct', 'position_sizing.value') with low/high bounds, optional "
            "step, and is_int. objective is a metric to maximize (default 'sharpe'); "
            "n_trials caps the Optuna search; walk_forward_folds>0 adds out-of-sample "
            "folds (the train/test gap is the overfitting signal). since/until and "
            "initial_cash/slippage_pct/seed mirror run_backtest. Pure simulation -- sync "
            "the data first; no orders, no network."
        ),
        structured_output=True,
    )
    def optimize_strategy_tool(
        strategy_name: str,
        params: list[ParamSpec],
        objective: str = "sharpe",
        n_trials: int = 50,
        walk_forward_folds: int = 0,
        since: datetime | None = None,
        until: datetime | None = None,
        initial_cash: float | None = None,
        slippage_pct: float | None = None,
        seed: int | None = None,
    ) -> OptimizeResult:
        """Sweep ``params`` for the saved strategy ``strategy_name`` and return the result.

        Loads the spec, reads the spec's cached bars (windowed by since/until) from
        the OHLCV store, and runs the native Optuna sweep (the source-of-truth path).
        Raises a redacted not-found ``ValidationError`` for a missing strategy and a
        redacted "sync first" ``ValidationError`` if the window has no cached bars.
        """
        spec = strategy_store.load(strategy_name)
        config = _build_config(initial_cash=initial_cash, slippage_pct=slippage_pct, seed=seed)
        bars = store.read_bars(
            DatasetKey(exchange=spec.exchange, symbol=spec.symbol, timeframe=spec.timeframe),
            since=since,
            until=until,
        )
        return optimize_strategy(
            spec,
            bars,
            params,
            objective=objective,
            n_trials=n_trials,
            walk_forward_folds=walk_forward_folds,
            config=config,
        )

    @app.tool(
        name="generate_tearsheet",
        title="Generate a tear sheet",
        description=(
            "Render a quantstats HTML tear sheet for a saved backtest report and return "
            "the file path plus the report's metrics. Never fails: if quantstats is "
            "unavailable it degrades gracefully (html_path is null, metrics still "
            "returned, with a note). Loads the report from the local store -- no network."
        ),
        structured_output=True,
    )
    def generate_tearsheet_tool(report_id: str, title: str | None = None) -> TearsheetResult:
        """Generate a tear sheet for the saved report ``report_id`` (never raises).

        Loads the report (raising a redacted not-found error if absent), then renders
        the tear sheet -- which degrades gracefully when quantstats is missing.
        """
        report = backtest_store.load(report_id)
        return generate_tearsheet(report, title=title)
