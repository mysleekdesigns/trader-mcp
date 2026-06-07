"""Tests for the Phase 4 backtest & optimize MCP tool surface (PRD §5.2, §6).

The five tools (``run_backtest``, ``get_backtest_report``, ``compare_backtests``,
``optimize_strategy``, ``generate_tearsheet``) wrap the event-driven engine and
return typed Pydantic v2 models, so every structured tool output must validate
against its declared engine model and every tool must advertise an ``outputSchema``.

Fully offline & deterministic: there is no network or execution path here -- a
backtest is pure simulation over the local OHLCV cache. We seed a tmp ``data_dir``
(via ``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()``, the Phase 2/3 pattern)
with one small synthetic dataset AND one saved strategy, build the app, then drive
the tools through the FastMCP registry asserting typed output. Synthetic bars are
seeded with a fixed numpy Generator -- never wall-clock.

quantstats / optuna emit warnings; ``filterwarnings = ["error"]`` is strict, so the
optimize/tearsheet tests narrow-filter only the specific categories they trip.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trader_mcp.config import get_settings
from trader_mcp.data.models import DatasetKey
from trader_mcp.data.store import OHLCVStore
from trader_mcp.engine import (
    BacktestComparison,
    BacktestReport,
    OptimizeResult,
    TearsheetResult,
)
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.server.app import build_app
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
    StrategyStore,
)

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"
_STRATEGY = "sma-cross-tooltest"
_N_BARS = 400
_SEED = 99


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-output dict from a FastMCP ``call_tool`` result."""
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def _seed_bars() -> list[OHLCVBar]:
    """Deterministic synthetic OHLCV with many SMA crossings (seeded Generator)."""
    rng = np.random.default_rng(_SEED)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 8.0 * np.sin(t / 15.0) + rng.normal(0, 1, _N_BARS).cumsum() * 0.1, 1.0
    )
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        close = float(c)
        open_px = prev if i > 0 else close
        bars.append(
            OHLCVBar(
                timestamp=t0 + timedelta(hours=i),
                open=open_px,
                high=max(open_px, close) * 1.002,
                low=min(open_px, close) * 0.998,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return bars


def _spec(name: str = _STRATEGY) -> StrategySpec:
    """A long/short SMA cross spec keyed to the seeded (exchange, symbol, tf)."""
    return StrategySpec(
        name=name,
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 10}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 30}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=20.0),
        risk=RiskLimits(stop_loss_pct=3.0, take_profit_pct=6.0),
        fees=Fees(),
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Root every process-wide store at a tmp dir for the test's duration."""
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def seeded_app(data_dir: Path) -> Any:
    """Seed one dataset + one saved strategy into the tmp dir, then build the app."""
    OHLCVStore(data_dir).upsert_bars(
        DatasetKey(exchange=_EXCHANGE, symbol=_SYMBOL, timeframe=_TIMEFRAME), _seed_bars()
    )
    StrategyStore(data_dir).save(_spec())
    return build_app()


@pytest.fixture
def app_no_data(data_dir: Path) -> Any:
    """App with a saved strategy but NO cached data (the 'sync first' path)."""
    StrategyStore(data_dir).save(_spec())
    return build_app()


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
PHASE4_TOOLS = (
    "run_backtest",
    "get_backtest_report",
    "compare_backtests",
    "optimize_strategy",
    "generate_tearsheet",
)


async def test_phase4_tools_registered() -> None:
    names = {t.name for t in await build_app().list_tools()}
    assert set(PHASE4_TOOLS) <= names


async def test_phase4_tools_advertise_structured_output_schema() -> None:
    by_name = {t.name: t for t in await build_app().list_tools()}
    for name in PHASE4_TOOLS:
        tool = by_name[name]
        assert tool.outputSchema is not None, f"{name} must advertise an outputSchema"
        assert tool.outputSchema.get("type") == "object"


async def test_phase4_adds_no_live_arming_surface() -> None:
    """No Phase-6 live-arming/kill-switch surface exists yet (safe-by-default).

    Phase 5 legitimately introduces ``place_order``/``deploy_strategy`` (gated,
    dry-run by default), so the still-true invariant to assert is the absence of the
    real-money arming and kill-switch tools, which do not land until Phase 6.
    """
    names = {t.name for t in await build_app().list_tools()}
    assert names.isdisjoint({"arm_live_trading", "kill_switch"})


# --------------------------------------------------------------------------- #
# run_backtest -> persisted report -> get_backtest_report round trip
# --------------------------------------------------------------------------- #
async def test_run_backtest_returns_report_and_persists(seeded_app: Any) -> None:
    report = BacktestReport.model_validate(
        _structured(await seeded_app.call_tool("run_backtest", {"strategy_name": _STRATEGY}))
    )
    assert report.strategy_name == _STRATEGY
    assert report.exchange == _EXCHANGE
    assert report.symbol == _SYMBOL
    assert report.timeframe == _TIMEFRAME
    assert report.bars == _N_BARS
    assert report.report_id

    # The report was persisted and is loadable by id via get_backtest_report.
    loaded = BacktestReport.model_validate(
        _structured(
            await seeded_app.call_tool("get_backtest_report", {"report_id": report.report_id})
        )
    )
    assert loaded.report_id == report.report_id
    assert loaded.final_equity == report.final_equity
    assert len(loaded.trades) == len(report.trades)


async def test_get_backtest_report_unknown_id_raises(seeded_app: Any) -> None:
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await seeded_app.call_tool("get_backtest_report", {"report_id": "deadbeef"})
    assert "deadbeef" in str(excinfo.value) or "not found" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# Tool-level determinism: identical inputs -> identical report_id + metrics
# --------------------------------------------------------------------------- #
async def test_run_backtest_is_deterministic_through_the_tool(seeded_app: Any) -> None:
    a = BacktestReport.model_validate(
        _structured(await seeded_app.call_tool("run_backtest", {"strategy_name": _STRATEGY}))
    )
    b = BacktestReport.model_validate(
        _structured(await seeded_app.call_tool("run_backtest", {"strategy_name": _STRATEGY}))
    )
    assert a.report_id == b.report_id
    assert a.final_equity == b.final_equity
    assert a.metrics.model_dump() == b.metrics.model_dump()


# --------------------------------------------------------------------------- #
# compare_backtests picks a best
# --------------------------------------------------------------------------- #
async def test_compare_backtests_picks_best(seeded_app: Any) -> None:
    """Two reports for the same strategy at different cash bases -> a comparison.

    Distinct configs yield distinct report_ids (content-addressed), so we get two
    rows; the comparison must name a winning report_id under the objective.
    """
    r1 = BacktestReport.model_validate(
        _structured(
            await seeded_app.call_tool(
                "run_backtest", {"strategy_name": _STRATEGY, "initial_cash": 10_000}
            )
        )
    )
    r2 = BacktestReport.model_validate(
        _structured(
            await seeded_app.call_tool(
                "run_backtest", {"strategy_name": _STRATEGY, "initial_cash": 25_000}
            )
        )
    )
    assert r1.report_id != r2.report_id

    comparison = BacktestComparison.model_validate(
        _structured(
            await seeded_app.call_tool(
                "compare_backtests",
                {"report_ids": [r1.report_id, r2.report_id], "objective": "total_return_pct"},
            )
        )
    )
    assert len(comparison.reports) == 2
    assert comparison.best in {r1.report_id, r2.report_id}
    assert comparison.objective == "total_return_pct"


# --------------------------------------------------------------------------- #
# optimize_strategy: sweep + walk-forward
# --------------------------------------------------------------------------- #
@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_optimize_strategy_returns_best_params(seeded_app: Any) -> None:
    result = OptimizeResult.model_validate(
        _structured(
            await seeded_app.call_tool(
                "optimize_strategy",
                {
                    "strategy_name": _STRATEGY,
                    "params": [
                        {"path": "indicators.fast.length", "low": 5, "high": 15, "is_int": True}
                    ],
                    "objective": "sharpe",
                    "n_trials": 4,  # small: keep it fast & deterministic
                },
            )
        )
    )
    assert result.strategy_name == _STRATEGY
    assert result.objective == "sharpe"
    assert "indicators.fast.length" in result.best_params
    assert result.trials  # at least one evaluated trial
    assert result.folds == []  # no walk-forward requested
    assert result.backend == "native"


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_optimize_strategy_walk_forward_produces_folds(seeded_app: Any) -> None:
    result = OptimizeResult.model_validate(
        _structured(
            await seeded_app.call_tool(
                "optimize_strategy",
                {
                    "strategy_name": _STRATEGY,
                    "params": [
                        {"path": "indicators.fast.length", "low": 5, "high": 15, "is_int": True}
                    ],
                    "objective": "sharpe",
                    "n_trials": 3,
                    # Anchored walk-forward: N segments yield N-1 train/test folds
                    # (fold k trains on [0:seg*k], tests on the next segment), so 3
                    # segments produce 2 out-of-sample folds.
                    "walk_forward_folds": 3,
                },
            )
        )
    )
    assert len(result.folds) == 2
    assert [f.fold for f in result.folds] == [1, 2]
    for fold in result.folds:
        assert "indicators.fast.length" in fold.best_params
        # Each fold reports a train and a (recomputed) out-of-sample test value.
        assert isinstance(fold.train_value, float)
        assert isinstance(fold.test_value, float)


# --------------------------------------------------------------------------- #
# generate_tearsheet: never raises; metrics always present
# --------------------------------------------------------------------------- #
@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_generate_tearsheet_returns_metrics(seeded_app: Any) -> None:
    """Tear sheet returns the report's metrics; html_path is optional (graceful
    degradation if quantstats is unavailable). The tool must never raise.
    """
    report = BacktestReport.model_validate(
        _structured(await seeded_app.call_tool("run_backtest", {"strategy_name": _STRATEGY}))
    )
    result = TearsheetResult.model_validate(
        _structured(
            await seeded_app.call_tool("generate_tearsheet", {"report_id": report.report_id})
        )
    )
    assert result.report_id == report.report_id
    # metrics always echoed back, even when quantstats degrades (html_path None).
    assert result.metrics.trade_count == report.metrics.trade_count
    assert result.html_path is None or isinstance(result.html_path, str)


# --------------------------------------------------------------------------- #
# Error model: missing strategy / un-synced data surface as validation errors
# --------------------------------------------------------------------------- #
async def test_run_backtest_unknown_strategy_raises(seeded_app: Any) -> None:
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await seeded_app.call_tool("run_backtest", {"strategy_name": "no-such-strategy"})
    msg = str(excinfo.value).lower()
    assert "no-such-strategy" in msg or "no saved strategy" in msg or "not found" in msg


async def test_run_backtest_without_cached_data_raises_sync_first(app_no_data: Any) -> None:
    """A strategy with no cached bars surfaces the engine's 'sync first' error."""
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app_no_data.call_tool("run_backtest", {"strategy_name": _STRATEGY})
    msg = str(excinfo.value).lower()
    assert "sync" in msg or "no cached" in msg or "no bars" in msg
