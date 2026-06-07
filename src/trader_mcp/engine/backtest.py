"""Backtest orchestrator: spec + cached bars -> a typed, reproducible report.

This is the public entry point the MCP server wraps as ``run_backtest``. It wires
the four engine stages together, in order, with no look-ahead anywhere:

    df_from_bars -> compute_indicators (via the interpreter's ``prepare``)
    -> SpecInterpreter.signals (the ONE shared signal core, vectorized mode)
    -> SimulatedBroker.run (deterministic fills + equity curve)
    -> compute_metrics (native) -> BacktestReport.

DETERMINISM (PRD §6 exit criterion): the same ``(spec, bars, config)`` always
produces the identical report and the identical :attr:`report_id`. There is no
randomness in the fill path; the RNG seed in the config is recorded (and used by
the optimizer) but the single backtest is deterministic regardless. ``report_id``
is a stable SHA-256 over the spec JSON, the bars' (timestamp, OHLCV) tuples, and
the config JSON -- so it doubles as a content address for caching/resources.

Fill convention (stated here AND in :mod:`trader_mcp.engine.interpreter` /
:mod:`trader_mcp.engine.broker`): a signal derived from the CLOSE of bar ``t`` is
executed at the OPEN of bar ``t+1`` (backtesting.py's default next-bar-open fill),
except intrabar stop-loss/take-profit which fill at their price level on bar ``t``.
This is the convention QA configures backtesting.py to match for cross-validation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.engine.broker import SimulatedBroker
from trader_mcp.engine.interpreter import SpecInterpreter
from trader_mcp.engine.metrics import compute_metrics
from trader_mcp.engine.models import BacktestConfig, BacktestReport

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trader_mcp.engine.models import SimulatedTrade
    from trader_mcp.exchanges.models import OHLCVBar, OHLCVResult
    from trader_mcp.strategy import StrategySpec


def _coerce_bars(bars_or_result: Sequence[OHLCVBar] | OHLCVResult) -> list[OHLCVBar]:
    """Accept either a bar sequence or an :class:`OHLCVResult` and return its bars.

    Keeps :func:`run_backtest` pure and offline-testable: the caller can pass
    already-loaded bars (no store, no network) or the result object the data
    pipeline returns.
    """
    bars = getattr(bars_or_result, "bars", None)
    if bars is not None:
        return list(bars)
    return list(bars_or_result)  # type: ignore[arg-type]


def _report_id(spec: StrategySpec, bars: list[OHLCVBar], config: BacktestConfig) -> str:
    """Deterministic content hash of (spec, data window, config).

    Stable across runs and processes: the same inputs always hash to the same id,
    so it can address a cached report resource. Uses the canonical spec/config JSON
    plus a compact digest of every bar's (timestamp, OHLCV).
    """
    hasher = hashlib.sha256()
    hasher.update(spec.model_dump_json().encode("utf-8"))
    hasher.update(config.model_dump_json().encode("utf-8"))
    for b in bars:
        hasher.update(
            f"{b.timestamp.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume};".encode()
        )
    return hasher.hexdigest()[:16]


def run_backtest(
    spec: StrategySpec,
    bars_or_result: Sequence[OHLCVBar] | OHLCVResult,
    *,
    config: BacktestConfig | None = None,
) -> BacktestReport:
    """Run a deterministic backtest of ``spec`` over cached ``bars`` and report.

    PURE: takes already-loaded bars (or an :class:`OHLCVResult`) -- no store, no
    network, no credentials. The single source of truth for backtest numbers; the
    SAME interpreter feeds the live path in Phase 5.

    Args:
        spec: The validated declarative strategy (read-only; never mutated).
        bars_or_result: Ascending UTC OHLCV bars, or the data pipeline's
            :class:`OHLCVResult`.
        config: Fill/realism + reproducibility knobs (defaults to
            :class:`BacktestConfig` defaults: $10k, no slippage, seed 0).

    Returns:
        A :class:`BacktestReport` with metrics, trades, the per-bar equity curve,
        and a deterministic ``report_id``.
    """
    cfg = config or BacktestConfig()
    bars = _coerce_bars(bars_or_result)

    from trader_mcp.indicators import df_from_bars

    df = df_from_bars(bars)
    interpreter = SpecInterpreter(spec)
    prepared = interpreter.prepare(df)
    signals = interpreter.signals(prepared)

    bar_ms = timeframe_ms(spec.timeframe)
    broker = SimulatedBroker(spec, cfg)
    trades, equity_curve = broker.run(prepared, signals, bar_ms)

    metrics = compute_metrics(
        equity_curve,
        trades,
        initial_cash=cfg.initial_cash,
        timeframe=spec.timeframe,
        total_bars=len(bars),
    )

    start = bars[0].timestamp if bars else None
    end = bars[-1].timestamp if bars else None
    final_equity = equity_curve[-1].equity if equity_curve else cfg.initial_cash

    note = _build_note(spec, trades)

    return BacktestReport(
        report_id=_report_id(spec, bars, cfg),
        strategy_name=spec.name,
        exchange=spec.exchange,
        symbol=spec.symbol,
        timeframe=spec.timeframe,
        strategy_type=spec.strategy_type,
        start=start,
        end=end,
        bars=len(bars),
        initial_cash=cfg.initial_cash,
        final_equity=final_equity,
        metrics=metrics,
        trades=trades,
        equity_curve=equity_curve,
        config=cfg,
        created=datetime.now(tz=UTC),
        note=note,
    )


def run_backtest_from_store(
    spec: StrategySpec,
    *,
    store: object | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    config: BacktestConfig | None = None,
) -> BacktestReport:
    """Convenience: load cached bars for ``spec`` from a store, then backtest.

    A thin wrapper over :func:`run_backtest` for callers (e.g. the MCP tool) that
    want to read from the cache rather than pass bars. Keeps the pure path intact:
    it merely resolves bars and delegates. ``store`` defaults to a fresh
    :class:`trader_mcp.data.OHLCVStore`.

    Raises:
        trader_mcp.errors.ValidationError: if the dataset has no cached bars in the
            requested window (so a user gets a clear "sync first" message rather
            than an empty backtest).
    """
    from trader_mcp.data import DatasetKey, OHLCVStore
    from trader_mcp.errors import ValidationError

    ohlcv_store = store if store is not None else OHLCVStore()
    key = DatasetKey(exchange=spec.exchange, symbol=spec.symbol, timeframe=spec.timeframe)
    result = ohlcv_store.read_bars(key, since=since, until=until)  # type: ignore[attr-defined]
    if result.count == 0:
        raise ValidationError(
            f"No cached bars for {spec.exchange}:{spec.symbol} {spec.timeframe} in the "
            "requested window. Run sync_history first.",
            details={
                "kind": "no_cached_data",
                "exchange": spec.exchange,
                "symbol": spec.symbol,
                "timeframe": spec.timeframe,
            },
        )
    return run_backtest(spec, result, config=config)


def _build_note(spec: StrategySpec, trades: Sequence[SimulatedTrade]) -> str | None:
    """Assemble advisory notes (assumptions surfaced to the user)."""
    notes: list[str] = []
    if ":" in spec.symbol:
        notes.append(
            "Perp funding modeled as a flat assumed rate (no historical funding "
            "stream in the OHLCV cache)."
        )
    if any(t.exit_reason == "end_of_data" for t in trades):
        notes.append("One or more positions were force-closed at the end of the data window.")
    return " ".join(notes) if notes else None
