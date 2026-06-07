"""Tests for the Phase 4 backtest-report MCP resources (PRD §5.2 resources).

The catalog resource (``backtest://catalog``) and the per-report template
(``backtest://{report_id}``) are read-only views over the local backtest-report
store. These tests seed a tmp data dir with a dataset + a saved strategy, run a
backtest through the tool to persist a report, then assert the resources list and
read it back as typed JSON. Mirrors ``tests/test_dataset_resources.py`` /
``tests/test_strategy_resources.py``.

Fully offline & deterministic: synthetic bars seeded with a fixed numpy Generator;
stores rooted at a tmp dir via ``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trader_mcp.config import get_settings
from trader_mcp.data.models import DatasetKey
from trader_mcp.data.store import OHLCVStore
from trader_mcp.engine import BacktestReport
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.server.app import build_app
from trader_mcp.server.resources import (
    BACKTEST_CATALOG_URI,
    BACKTEST_URI_TEMPLATE,
    backtest_uri,
)
from trader_mcp.server.schemas import BacktestCatalogResult
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
_STRATEGY = "sma-cross-resourcetest"
_N_BARS = 300
# Seed 3 + the choppy generator below yields a report with BOTH winning AND losing
# trades, so metrics.profit_factor is FINITE -- a deliberate choice: an all-wins
# report has profit_factor == inf, which the per-report resource currently
# serializes to JSON null and CANNOT round-trip (see
# test_per_report_resource_with_infinite_profit_factor_is_broken below, a localized
# repro of a real server bug). The happy-path round-trip tests therefore use a
# finite-metric report so they assert the resource contract, not the bug.
_SEED = 3


def _seed_bars() -> list[OHLCVBar]:
    """Deterministic choppy OHLCV (seeded) that produces winners AND losers."""
    rng = np.random.default_rng(_SEED)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 5.0 * np.sin(t / 8.0) + rng.normal(0, 2.0, _N_BARS).cumsum() * 0.4, 1.0
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
                high=max(open_px, close) * 1.003,
                low=min(open_px, close) * 0.997,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return bars


def _spec() -> StrategySpec:
    """A long/short SMA cross with signal-only exits (no SL/TP), so a choppy series
    realizes losing trades and ``profit_factor`` stays finite/JSON-round-trippable.
    """
    return StrategySpec(
        name=_STRATEGY,
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 5}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 20}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=20.0),
        risk=RiskLimits(),
        fees=Fees(),
    )


def _read_text(contents: Any) -> str:
    """Extract the single text payload from a FastMCP ``read_resource`` result."""
    items = list(contents)
    assert len(items) == 1
    payload = items[0].content
    assert isinstance(payload, str)
    return payload


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def seeded_app(data_dir: Path) -> Any:
    OHLCVStore(data_dir).upsert_bars(
        DatasetKey(exchange=_EXCHANGE, symbol=_SYMBOL, timeframe=_TIMEFRAME), _seed_bars()
    )
    StrategyStore(data_dir).save(_spec())
    return build_app()


async def _run(app: Any) -> str:
    """Run a backtest through the tool (persisting a report) and return its id."""
    result = await app.call_tool("run_backtest", {"strategy_name": _STRATEGY})
    structured = result[1]
    assert isinstance(structured, dict)
    return BacktestReport.model_validate(structured).report_id


# --------------------------------------------------------------------------- #
# Resource advertisement
# --------------------------------------------------------------------------- #
async def test_backtest_catalog_resource_is_listed() -> None:
    uris = {str(r.uri) for r in await build_app().list_resources()}
    assert BACKTEST_CATALOG_URI in uris


async def test_backtest_report_template_is_listed() -> None:
    templates = {t.uriTemplate for t in await build_app().list_resource_templates()}
    assert BACKTEST_URI_TEMPLATE in templates


# --------------------------------------------------------------------------- #
# Catalog: empty before any run, lists saved reports with resolvable URIs after
# --------------------------------------------------------------------------- #
async def test_catalog_empty_before_any_run(seeded_app: Any) -> None:
    payload = _read_text(await seeded_app.read_resource(BACKTEST_CATALOG_URI))
    result = BacktestCatalogResult.model_validate_json(payload)
    assert result.count == 0
    assert result.reports == []


async def test_catalog_lists_saved_report_with_resolvable_uri(seeded_app: Any) -> None:
    report_id = await _run(seeded_app)
    payload = _read_text(await seeded_app.read_resource(BACKTEST_CATALOG_URI))
    result = BacktestCatalogResult.model_validate_json(payload)

    assert result.count == 1
    entry = result.reports[0]
    assert entry.report_id == report_id
    assert entry.strategy_name == _STRATEGY
    assert entry.exchange == _EXCHANGE
    assert entry.symbol == _SYMBOL
    assert entry.timeframe == _TIMEFRAME
    # Each entry carries a resolvable per-report resource URI.
    assert entry.resource_uri == backtest_uri(report_id)
    assert entry.resource_uri == f"backtest://{report_id}"


# --------------------------------------------------------------------------- #
# Per-report template returns the full BacktestReport JSON
# --------------------------------------------------------------------------- #
async def test_per_report_resource_returns_full_report(seeded_app: Any) -> None:
    report_id = await _run(seeded_app)
    payload = _read_text(await seeded_app.read_resource(f"backtest://{report_id}"))
    report = BacktestReport.model_validate_json(payload)

    assert report.report_id == report_id
    assert report.strategy_name == _STRATEGY
    assert report.bars == _N_BARS
    # The full report carries metrics, the equity curve, the trades, and the config.
    assert len(report.equity_curve) == _N_BARS
    assert report.config is not None
    assert report.metrics is not None


async def test_per_report_resource_round_trips_via_catalog_uri(seeded_app: Any) -> None:
    """The catalog's advertised resource_uri resolves to that exact report."""
    report_id = await _run(seeded_app)
    catalog = BacktestCatalogResult.model_validate_json(
        _read_text(await seeded_app.read_resource(BACKTEST_CATALOG_URI))
    )
    uri = catalog.reports[0].resource_uri
    report = BacktestReport.model_validate_json(_read_text(await seeded_app.read_resource(uri)))
    assert report.report_id == report_id


async def test_unknown_report_resource_errors(seeded_app: Any) -> None:
    """An unknown report id raises (redacted) rather than returning junk."""
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await seeded_app.read_resource("backtest://deadbeef")
    assert "deadbeef" in str(excinfo.value) or "not found" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# LOCALIZED REPRO of a real server bug (orchestrator: route to mcp-server lane).
# --------------------------------------------------------------------------- #
def _all_wins_spec() -> StrategySpec:
    """An SMA cross with a take-profit: on a clean uptrend every trade wins, so
    metrics.profit_factor == inf (documented: wins, no losses -> inf).
    """
    return StrategySpec(
        name="all-wins-resourcetest",
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


def _all_wins_bars() -> list[OHLCVBar]:
    """A clean trending+oscillating series (seeded) on which every trade wins."""
    rng = np.random.default_rng(7)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 8.0 * np.sin(t / 14.0) + rng.normal(0, 1, _N_BARS).cumsum() * 0.1, 1.0
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


async def test_per_report_resource_with_infinite_profit_factor_round_trips(
    data_dir: Path,
) -> None:
    """Regression: the per-report resource round-trips an INFINITE metric cleanly.

    An all-wins report has ``profit_factor == inf`` (documented: wins, no losses).
    The resource now serializes via ``json.dumps(report.model_dump(mode="json"))``,
    emitting the ``Infinity`` literal that re-parses back to ``float('inf')`` --
    rather than ``model_dump_json()``'s ``null``, which could not re-parse into the
    required float. This pins that fix: seed an all-wins report, run it through the
    tool to persist, read it back via the backtest:// resource, and assert it
    re-parses into a BacktestReport whose ``profit_factor`` is still infinite.
    """
    OHLCVStore(data_dir).upsert_bars(
        DatasetKey(exchange=_EXCHANGE, symbol=_SYMBOL, timeframe=_TIMEFRAME), _all_wins_bars()
    )
    StrategyStore(data_dir).save(_all_wins_spec())
    app = build_app()

    result = await app.call_tool("run_backtest", {"strategy_name": "all-wins-resourcetest"})
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    report = BacktestReport.model_validate(structured)
    assert math.isinf(report.metrics.profit_factor), "repro requires an all-wins report"

    payload = _read_text(await app.read_resource(f"backtest://{report.report_id}"))
    reparsed = BacktestReport.model_validate_json(payload)
    assert math.isinf(reparsed.metrics.profit_factor)
    assert reparsed.report_id == report.report_id
