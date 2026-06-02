"""Unit tests for the typed data-pipeline models (frozen, strict)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from trader_mcp.data.models import (
    DatasetInfo,
    DatasetInspection,
    DatasetKey,
    Gap,
    SyncResult,
)

_T = datetime(2024, 1, 1, tzinfo=UTC)


def test_dataset_key_is_frozen() -> None:
    key = DatasetKey(exchange="coinbase", symbol="BTC/USD", timeframe="1h")
    with pytest.raises(PydanticValidationError):
        key.symbol = "ETH/USD"  # type: ignore[misc]


def test_dataset_key_rejects_unknown_exchange() -> None:
    with pytest.raises(PydanticValidationError):
        DatasetKey(exchange="binance", symbol="BTC/USD", timeframe="1h")  # type: ignore[arg-type]


def test_dataset_key_forbids_extra_fields() -> None:
    # Build kwargs dynamically so the type checkers don't flag the deliberate
    # extra field; the point is that pydantic rejects it at runtime (extra=forbid).
    bad_kwargs: dict[str, str] = {
        "exchange": "coinbase",
        "symbol": "BTC/USD",
        "timeframe": "1h",
        "extra": "nope",
    }
    with pytest.raises(PydanticValidationError):
        DatasetKey(**bad_kwargs)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


def test_gap_model() -> None:
    gap = Gap(start=_T, end=_T, missing_bars=3)
    assert gap.missing_bars == 3


def test_dataset_info_defaults() -> None:
    info = DatasetInfo(exchange="coinbase", symbol="BTC/USD", timeframe="1h", row_count=0)
    assert info.start is None
    assert info.end is None
    assert info.last_synced is None


def test_dataset_inspection_fields() -> None:
    insp = DatasetInspection(
        exchange="coinbase",
        symbol="BTC/USD",
        timeframe="1h",
        row_count=10,
        expected_bars=12,
        missing_bars=2,
        gaps=[Gap(start=_T, end=_T, missing_bars=2)],
        duplicates_removed=1,
        monotonic_ok=True,
        start=_T,
        end=_T,
    )
    assert insp.missing_bars == 2
    assert len(insp.gaps) == 1


def test_sync_result_status_literal() -> None:
    result = SyncResult(
        exchange="coinbase",
        symbol="BTC/USD",
        timeframe="1h",
        bars_added=5,
        bars_total=5,
        requested_since=_T,
        requested_until=None,
        start=_T,
        end=_T,
        gaps_detected=0,
        gaps_repaired=0,
        pages_fetched=1,
        status="ok",
    )
    assert result.status == "ok"
    assert result.note is None


def test_sync_result_rejects_bad_status() -> None:
    with pytest.raises(PydanticValidationError):
        SyncResult(
            exchange="coinbase",
            symbol="BTC/USD",
            timeframe="1h",
            bars_added=0,
            bars_total=0,
            requested_since=_T,
            requested_until=None,
            start=None,
            end=None,
            gaps_detected=0,
            gaps_repaired=0,
            pages_fetched=0,
            status="broken",  # type: ignore[arg-type]
        )
