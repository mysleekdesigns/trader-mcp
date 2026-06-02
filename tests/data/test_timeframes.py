"""Unit tests for the canonical timeframe map (single source of cadence truth)."""

from __future__ import annotations

import pytest

from trader_mcp.data.timeframes import (
    SUPPORTED_TIMEFRAMES,
    TIMEFRAME_MS,
    is_supported_timeframe,
    timeframe_ms,
)
from trader_mcp.errors import ValidationError


def test_supported_list_matches_map_and_is_ordered() -> None:
    assert list(TIMEFRAME_MS) == SUPPORTED_TIMEFRAMES
    durations = [TIMEFRAME_MS[tf] for tf in SUPPORTED_TIMEFRAMES]
    assert durations == sorted(durations), "timeframes must be ordered shortest->longest"


@pytest.mark.parametrize(
    ("timeframe", "expected_ms"),
    [
        ("1m", 60_000),
        ("5m", 300_000),
        ("15m", 900_000),
        ("30m", 1_800_000),
        ("1h", 3_600_000),
        ("4h", 14_400_000),
        ("1d", 86_400_000),
        ("1w", 604_800_000),
    ],
)
def test_timeframe_ms_exact_durations(timeframe: str, expected_ms: int) -> None:
    assert timeframe_ms(timeframe) == expected_ms


def test_is_supported_timeframe() -> None:
    assert is_supported_timeframe("1h") is True
    assert is_supported_timeframe("3m") is False
    assert is_supported_timeframe("") is False


def test_timeframe_ms_unknown_raises_validation_error() -> None:
    with pytest.raises(ValidationError) as exc:
        timeframe_ms("3m")
    assert exc.value.details["kind"] == "unsupported_timeframe"
    assert exc.value.details["timeframe"] == "3m"
    assert "supported" in exc.value.details
