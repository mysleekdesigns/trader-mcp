"""Unit tests for the pure data-quality functions (normalize/gaps/monotonic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trader_mcp.data.quality import (
    detect_gaps,
    expected_bar_count,
    is_monotonic,
    normalize,
)
from trader_mcp.errors import ValidationError
from trader_mcp.exchanges.models import OHLCVBar

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(hour: int, *, close: float | None = None) -> OHLCVBar:
    return OHLCVBar(
        timestamp=_BASE + timedelta(hours=hour),
        open=100.0,
        high=101.0,
        low=99.0,
        close=close if close is not None else 100.5,
        volume=10.0,
    )


def test_normalize_sorts_and_dedupes_keeping_last() -> None:
    bars = [_bar(2), _bar(0), _bar(1), _bar(1, close=999.0)]  # dup at hour 1
    cleaned, dupes = normalize(bars, "1h")
    assert dupes == 1
    assert [b.timestamp for b in cleaned] == [
        _BASE,
        _BASE + timedelta(hours=1),
        _BASE + timedelta(hours=2),
    ]
    # The *last* duplicate wins.
    assert cleaned[1].close == 999.0


def test_normalize_coerces_naive_to_utc() -> None:
    naive = OHLCVBar(
        timestamp=datetime(2024, 1, 1, 5, 0, 0),  # naive
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=3.0,
    )
    cleaned, _ = normalize([naive], "1h")
    assert cleaned[0].timestamp.tzinfo is not None
    assert cleaned[0].timestamp == datetime(2024, 1, 1, 5, 0, 0, tzinfo=UTC)


def test_normalize_unknown_timeframe_raises() -> None:
    with pytest.raises(ValidationError):
        normalize([_bar(0)], "3m")


def test_is_monotonic() -> None:
    assert is_monotonic([_bar(0), _bar(1), _bar(2)]) is True
    assert is_monotonic([_bar(0), _bar(0)]) is False  # equal -> not strict
    assert is_monotonic([_bar(2), _bar(1)]) is False  # descending
    assert is_monotonic([]) is True
    assert is_monotonic([_bar(0)]) is True


def test_detect_gaps_none_when_dense() -> None:
    bars = [_bar(h) for h in range(5)]
    assert detect_gaps(bars, "1h") == []


def test_detect_gaps_single_run() -> None:
    # hours 0,1,2 then 5,6 -> missing 3,4
    bars = [_bar(0), _bar(1), _bar(2), _bar(5), _bar(6)]
    gaps = detect_gaps(bars, "1h")
    assert len(gaps) == 1
    assert gaps[0].missing_bars == 2
    assert gaps[0].start == _BASE + timedelta(hours=3)
    assert gaps[0].end == _BASE + timedelta(hours=4)


def test_detect_gaps_multiple_runs() -> None:
    # 0,1 [gap 2] 3,4 [gap 5,6] 7
    bars = [_bar(0), _bar(1), _bar(3), _bar(4), _bar(7)]
    gaps = detect_gaps(bars, "1h")
    assert [g.missing_bars for g in gaps] == [1, 2]
    assert gaps[0].start == _BASE + timedelta(hours=2)
    assert gaps[1].start == _BASE + timedelta(hours=5)
    assert gaps[1].end == _BASE + timedelta(hours=6)


def test_expected_bar_count() -> None:
    assert expected_bar_count([], "1h") == 0
    assert expected_bar_count([_bar(0)], "1h") == 1
    # first..last spans hours 0..6 inclusive -> 7 expected even if some missing
    assert expected_bar_count([_bar(0), _bar(6)], "1h") == 7
