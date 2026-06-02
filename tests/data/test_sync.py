"""Sync-pipeline tests: pagination, resumability, page caps, and gap repair.

All fully offline: the paginating fake is injected behind the real adapter's
single client-construction seam (``_create_ccxt_client``), so bars flow through
the genuine :class:`ExchangeAdapter` normalization on their way into the store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests.data._ohlcv_fakes import PaginatingFakeCcxt
from trader_mcp.data import inspect_dataset, sync_history
from trader_mcp.data.models import DatasetKey
from trader_mcp.data.store import OHLCVStore
from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.errors import ExchangeError, ValidationError
from trader_mcp.exchanges.manager import ExchangeManager

# One year of 1h bars + a little headroom.
_TIMEFRAME = "1h"
_STEP_MS = timeframe_ms(_TIMEFRAME)
_YEAR_BARS = 24 * 365  # 8760
_START = datetime(2023, 1, 1, tzinfo=UTC)
_START_MS = int(_START.timestamp() * 1000)


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: PaginatingFakeCcxt) -> None:
    """Wire ``fake`` behind the adapter's client-construction seam + no-op sleep."""

    def factory(exchange_id: str, config: dict[str, object]) -> PaginatingFakeCcxt:
        return fake

    monkeypatch.setattr(adapter_module, "_create_ccxt_client", factory)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(adapter_module, "_sleep", no_sleep)


def _make_fake(*, bar_count: int = _YEAR_BARS, page_cap: int = 300) -> PaginatingFakeCcxt:
    return PaginatingFakeCcxt(
        timeframe=_TIMEFRAME,
        start_ms=_START_MS,
        bar_count=bar_count,
        page_cap=page_cap,
    )


@pytest.fixture
def store(tmp_path: Path) -> OHLCVStore:
    return OHLCVStore(tmp_path)


@pytest.fixture
def key() -> DatasetKey:
    return DatasetKey(exchange="coinbase", symbol="BTC/USD", timeframe=_TIMEFRAME)


# --------------------------------------------------------------------------- #
# Headline exit-criteria test (PRD §6 Phase 2 exit).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sync_one_year_paginates_resumes_repairs_and_serves(
    monkeypatch: pytest.MonkeyPatch,
    store: OHLCVStore,
    key: DatasetKey,
) -> None:
    """Sync >=1yr BTC/USD 1h on coinbase: page, resume, survive a cap, repair a gap."""
    fake = _make_fake(bar_count=_YEAR_BARS, page_cap=300)
    _install_fake(monkeypatch, fake)
    manager = ExchangeManager()
    until = _START + timedelta(milliseconds=_STEP_MS * (_YEAR_BARS - 1))

    try:
        # -- 1) Initial sync over a full year. --------------------------------
        result = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=1000,  # client asks for 1000; fake caps each page at 300
            store=store,
        )
        assert result.status == "ok"
        assert result.bars_added == _YEAR_BARS
        assert result.bars_total == _YEAR_BARS
        # Survived a per-page cap (300 < 1000) -> must have paged many times.
        assert result.pages_fetched > 1
        assert fake.page_cap < 1000
        assert result.gaps_detected == 0
        assert result.start == _START
        assert result.end == until

        # -- 2) Resumable: a second identical sync adds nothing. --------------
        calls_before = len(fake.fetch_calls)
        again = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=1000,
            store=store,
        )
        assert again.bars_added == 0
        assert again.bars_total == _YEAR_BARS
        assert again.status == "ok"
        # The resume cursor starts past the last cached bar, so it does not
        # re-page the entire year.
        assert len(fake.fetch_calls) - calls_before <= 2
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
async def test_sync_detects_and_repairs_injected_gap(
    monkeypatch: pytest.MonkeyPatch,
    store: OHLCVStore,
    key: DatasetKey,
) -> None:
    """Omit a window, confirm the gap is detected, then repair it and serve clean."""
    # A modest series so the test is fast; still pages under the cap.
    bar_count = 500
    fake = _make_fake(bar_count=bar_count, page_cap=100)
    _install_fake(monkeypatch, fake)
    manager = ExchangeManager()
    until = _START + timedelta(milliseconds=_STEP_MS * (bar_count - 1))

    # Punch a 5-bar gap (bars 200..204 inclusive) into the served data.
    gap_lo = _START_MS + 200 * _STEP_MS
    gap_hi = _START_MS + 204 * _STEP_MS
    fake.omit_window(gap_lo, gap_hi)

    try:
        # First sync: data has a hole. Disable repair so we can observe the gap.
        first = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=1000,
            repair_gaps=False,
            store=store,
        )
        assert first.gaps_detected == 1
        assert first.gaps_repaired == 0
        assert first.bars_total == bar_count - 5

        insp_before = inspect_dataset(store, key)
        assert insp_before.missing_bars == 5
        assert len(insp_before.gaps) == 1
        assert insp_before.gaps[0].missing_bars == 5

        # Heal the source, then re-sync with repair on.
        fake.clear_omission()
        repaired = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=1000,
            repair_gaps=True,
            store=store,
        )
        assert repaired.gaps_detected == 1
        assert repaired.gaps_repaired == 1
        assert repaired.bars_added == 5
        assert repaired.bars_total == bar_count
        assert repaired.status == "ok"

        # -- Serve it back to the backtester: clean, dense, monotonic, UTC. ---
        insp_after = inspect_dataset(store, key)
        assert insp_after.missing_bars == 0
        assert insp_after.gaps == []
        assert insp_after.monotonic_ok is True
        assert insp_after.duplicates_removed == 0

        served = store.read_bars(key)
        assert served.count == bar_count
        assert all(b.timestamp.tzinfo is not None for b in served.bars)
        timestamps = [b.timestamp for b in served.bars]
        assert timestamps == sorted(timestamps)
        # No duplicates.
        assert len(set(timestamps)) == bar_count
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
async def test_sync_repairs_gap_wider_than_page_limit_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
    store: OHLCVStore,
    key: DatasetKey,
) -> None:
    """A gap WIDER than page_limit is fully backfilled in a single sync_history call.

    With page_limit=50 and a ~120-bar hole, the repair must page within the gap
    window (>1 fetch) to close it -- a single-page repair would leave bars missing.
    """
    bar_count = 600
    page_limit = 50
    gap_width = 120  # > page_limit, so repair must page 3x within the gap
    # page_cap must not bind below page_limit, so the repair pages are capped at 50.
    fake = _make_fake(bar_count=bar_count, page_cap=200)
    _install_fake(monkeypatch, fake)
    manager = ExchangeManager()
    until = _START + timedelta(milliseconds=_STEP_MS * (bar_count - 1))

    # Punch a wide gap: bars 300..419 inclusive (120 bars).
    gap_first = 300
    gap_last = gap_first + gap_width - 1
    gap_lo = _START_MS + gap_first * _STEP_MS
    gap_hi = _START_MS + gap_last * _STEP_MS
    fake.omit_window(gap_lo, gap_hi)

    try:
        # First sync (repair off): observe the full-width hole.
        first = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=page_limit,
            repair_gaps=False,
            store=store,
        )
        assert first.gaps_detected == 1
        assert first.gaps_repaired == 0
        assert first.bars_total == bar_count - gap_width
        insp_before = inspect_dataset(store, key)
        assert insp_before.missing_bars == gap_width
        assert insp_before.gaps[0].missing_bars == gap_width

        # Heal the source, re-sync with repair on -- ONE call must close it fully.
        fake.clear_omission()
        repaired = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=page_limit,
            repair_gaps=True,
            store=store,
        )
        assert repaired.gaps_detected == 1
        assert repaired.gaps_repaired == 1
        assert repaired.bars_added == gap_width
        assert repaired.bars_total == bar_count
        assert repaired.status == "ok"

        # Fully dense and serveable.
        insp_after = inspect_dataset(store, key)
        assert insp_after.missing_bars == 0
        assert insp_after.gaps == []
        assert insp_after.monotonic_ok is True

        served = store.read_bars(key)
        assert served.count == bar_count
        timestamps = [b.timestamp for b in served.bars]
        assert len(set(timestamps)) == bar_count
        assert timestamps == sorted(timestamps)
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
async def test_sync_empty_window_returns_empty_status(
    monkeypatch: pytest.MonkeyPatch,
    store: OHLCVStore,
) -> None:
    """A window beyond the available series yields status='empty'."""
    fake = _make_fake(bar_count=10, page_cap=100)
    _install_fake(monkeypatch, fake)
    manager = ExchangeManager()
    far_future = _START + timedelta(days=3650)
    try:
        result = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=far_future,
            until=far_future + timedelta(days=1),
            store=store,
        )
        assert result.status == "empty"
        assert result.bars_total == 0
        assert result.bars_added == 0
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
async def test_sync_mid_run_error_is_partial_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    store: OHLCVStore,
    key: DatasetKey,
) -> None:
    """A mid-sync adapter error persists progress and returns status='partial'."""
    fake = _make_fake(bar_count=1000, page_cap=100)
    _install_fake(monkeypatch, fake)
    manager = ExchangeManager()
    until = _START + timedelta(milliseconds=_STEP_MS * 999)

    # Fail the fetch after a few successful pages.
    original = fake.fetch_ohlcv
    state = {"calls": 0}

    async def flaky(symbol: str, timeframe: str, since: int | None, limit: int):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 4:
            raise ExchangeError("coinbase fetch_ohlcv failed: network blip")
        return await original(symbol, timeframe, since, limit)

    monkeypatch.setattr(fake, "fetch_ohlcv", flaky)

    try:
        result = await sync_history(
            manager,
            "coinbase",
            "BTC/USD",
            _TIMEFRAME,
            since=_START,
            until=until,
            page_limit=1000,
            store=store,
        )
        assert result.status == "partial"
        assert result.note is not None
        # Progress before the failure is persisted (3 pages * 100 bars).
        assert result.bars_total > 0
        assert store.row_count(key) == result.bars_total
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
async def test_sync_validates_inputs_before_network(
    monkeypatch: pytest.MonkeyPatch,
    store: OHLCVStore,
) -> None:
    """Caller-side input errors raise ValidationError before any fetch."""
    fake = _make_fake(bar_count=10)
    _install_fake(monkeypatch, fake)
    manager = ExchangeManager()
    try:
        with pytest.raises(ValidationError):
            await sync_history(manager, "coinbase", "BTC/USD", "3m", since=_START, store=store)
        with pytest.raises(ValidationError):
            await sync_history(
                manager,
                "coinbase",
                "BTC/USD",
                _TIMEFRAME,
                since=_START,
                until=_START - timedelta(hours=1),  # until <= since
                store=store,
            )
        with pytest.raises(ValidationError):
            await sync_history(
                manager,
                "coinbase",
                "BTC/USD",
                _TIMEFRAME,
                since=_START,
                page_limit=0,  # non-positive
                store=store,
            )
        # No network calls should have happened.
        assert fake.fetch_calls == []
    finally:
        await manager.aclose_all()
