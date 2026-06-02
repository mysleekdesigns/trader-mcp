"""Unit tests for the DuckDB + Parquet OHLCV store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trader_mcp.data import inspect_dataset, list_cached_datasets
from trader_mcp.data.models import DatasetKey
from trader_mcp.data.store import OHLCVStore, sanitize_symbol
from trader_mcp.exchanges.models import OHLCVBar

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(hour: int, *, close: float = 100.5) -> OHLCVBar:
    return OHLCVBar(
        timestamp=_BASE + timedelta(hours=hour),
        open=100.0,
        high=101.0,
        low=99.0,
        close=close,
        volume=10.0,
    )


@pytest.fixture
def store(tmp_path: Path) -> OHLCVStore:
    return OHLCVStore(tmp_path)


@pytest.fixture
def key() -> DatasetKey:
    return DatasetKey(exchange="coinbase", symbol="BTC/USD", timeframe="1h")


def test_construction_is_cheap_no_filesystem(tmp_path: Path) -> None:
    sub = tmp_path / "does-not-exist-yet"
    OHLCVStore(sub)
    assert not sub.exists(), "constructing a store must not create the data dir"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC/USD", "BTC-USD"),
        ("BTC/USD:USD", "BTC-USD-USD"),
        ("ETH/BTC", "ETH-BTC"),
    ],
)
def test_sanitize_symbol(symbol: str, expected: str) -> None:
    assert sanitize_symbol(symbol) == expected


def test_path_layout(store: OHLCVStore, key: DatasetKey) -> None:
    path = store.path_for(key)
    assert path.name == "1h.parquet"
    assert path.parent.name == "BTC-USD"
    assert path.parent.parent.name == "coinbase"


def test_upsert_and_read(store: OHLCVStore, key: DatasetKey) -> None:
    added = store.upsert_bars(key, [_bar(0), _bar(1), _bar(2)])
    assert added == 3
    assert store.exists(key)
    assert store.row_count(key) == 3
    res = store.read_bars(key)
    assert res.count == 3
    assert res.exchange == "coinbase"
    assert res.symbol == "BTC/USD"
    assert res.timeframe == "1h"
    # tz-aware UTC, ascending.
    assert all(b.timestamp.tzinfo is not None for b in res.bars)
    assert [b.timestamp for b in res.bars] == sorted(b.timestamp for b in res.bars)


def test_upsert_dedupes_and_is_idempotent(store: OHLCVStore, key: DatasetKey) -> None:
    store.upsert_bars(key, [_bar(0), _bar(1)])
    # Re-upsert overlapping + new; only hour 2 is new.
    added = store.upsert_bars(key, [_bar(1), _bar(2)])
    assert added == 1
    assert store.row_count(key) == 3
    # Fully idempotent: re-adding everything adds nothing.
    assert store.upsert_bars(key, [_bar(0), _bar(1), _bar(2)]) == 0


def test_upsert_last_write_wins_on_duplicate(store: OHLCVStore, key: DatasetKey) -> None:
    store.upsert_bars(key, [_bar(0, close=1.0)])
    store.upsert_bars(key, [_bar(0, close=999.0)])
    res = store.read_bars(key)
    assert res.count == 1
    assert res.bars[0].close == 999.0


def test_upsert_unsorted_input_is_sorted(store: OHLCVStore, key: DatasetKey) -> None:
    store.upsert_bars(key, [_bar(3), _bar(0), _bar(2), _bar(1)])
    res = store.read_bars(key)
    assert [b.timestamp for b in res.bars] == [_BASE + timedelta(hours=h) for h in range(4)]


def test_read_bars_since_until_limit(store: OHLCVStore, key: DatasetKey) -> None:
    store.upsert_bars(key, [_bar(h) for h in range(10)])
    windowed = store.read_bars(
        key, since=_BASE + timedelta(hours=2), until=_BASE + timedelta(hours=5)
    )
    assert [b.timestamp for b in windowed.bars] == [
        _BASE + timedelta(hours=h) for h in (2, 3, 4, 5)
    ]
    limited = store.read_bars(key, limit=3)
    assert limited.count == 3


def test_read_absent_dataset_is_empty(store: OHLCVStore, key: DatasetKey) -> None:
    res = store.read_bars(key)
    assert res.count == 0
    assert res.bars == []
    assert store.row_count(key) == 0
    assert store.exists(key) is False
    assert store.last_timestamp(key) is None


def test_last_timestamp(store: OHLCVStore, key: DatasetKey) -> None:
    store.upsert_bars(key, [_bar(0), _bar(5), _bar(2)])
    assert store.last_timestamp(key) == _BASE + timedelta(hours=5)


def test_dataset_info_and_last_synced(store: OHLCVStore, key: DatasetKey) -> None:
    store.upsert_bars(key, [_bar(0), _bar(1), _bar(2)])
    info = store.dataset_info(key)
    assert info.row_count == 3
    assert info.start == _BASE
    assert info.end == _BASE + timedelta(hours=2)
    assert info.last_synced is None  # not marked yet
    when = datetime(2024, 6, 1, tzinfo=UTC)
    store.mark_synced(key, when)
    assert store.dataset_info(key).last_synced == when
    assert store.last_synced(key) == when


def test_dataset_info_absent(store: OHLCVStore, key: DatasetKey) -> None:
    info = store.dataset_info(key)
    assert info.row_count == 0
    assert info.start is None
    assert info.end is None


def test_list_datasets_recovers_canonical_symbol(store: OHLCVStore) -> None:
    # A symbol with both separators must round-trip via the sidecar manifest.
    swap_key = DatasetKey(exchange="coinbase", symbol="BTC/USD:USD", timeframe="4h")
    spot_key = DatasetKey(exchange="kraken", symbol="ETH/USD", timeframe="1h")
    store.upsert_bars(swap_key, [_bar(0)])
    store.upsert_bars(spot_key, [_bar(0)])

    infos = list_cached_datasets(store)
    pairs = {(i.exchange, i.symbol, i.timeframe) for i in infos}
    assert ("coinbase", "BTC/USD:USD", "4h") in pairs
    assert ("kraken", "ETH/USD", "1h") in pairs


def test_list_datasets_empty(store: OHLCVStore) -> None:
    assert list_cached_datasets(store) == []


def test_inspect_dataset_reports_gaps(store: OHLCVStore, key: DatasetKey) -> None:
    # hours 0,1,2 then 5,6 -> missing 3,4
    store.upsert_bars(key, [_bar(0), _bar(1), _bar(2), _bar(5), _bar(6)])
    insp = inspect_dataset(store, key)
    assert insp.row_count == 5
    assert insp.expected_bars == 7
    assert insp.missing_bars == 2
    assert len(insp.gaps) == 1
    assert insp.gaps[0].missing_bars == 2
    assert insp.monotonic_ok is True
    assert insp.duplicates_removed == 0


def test_inspect_absent_dataset(store: OHLCVStore, key: DatasetKey) -> None:
    insp = inspect_dataset(store, key)
    assert insp.row_count == 0
    assert insp.expected_bars == 0
    assert insp.missing_bars == 0
    assert insp.gaps == []
    assert insp.monotonic_ok is True
