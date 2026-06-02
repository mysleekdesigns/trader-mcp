"""Unit tests for multi-timeframe OHLCV resampling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trader_mcp.data.resample import resample
from trader_mcp.errors import ValidationError
from trader_mcp.exchanges.models import OHLCVBar

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(hour: int, o: float, h: float, low: float, c: float, v: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp=_BASE + timedelta(hours=hour),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def test_resample_empty_returns_empty() -> None:
    assert resample([], "1h", "4h") == []


def test_resample_1h_to_4h_aggregation() -> None:
    # One 4h bucket (hours 0..3) with known OHLCV extremes.
    bars = [
        _bar(0, o=100, h=110, low=95, c=105, v=10),
        _bar(1, o=105, h=120, low=100, c=108, v=20),
        _bar(2, o=108, h=115, low=90, c=112, v=30),
        _bar(3, o=112, h=118, low=104, c=116, v=40),
    ]
    out = resample(bars, "1h", "4h")
    assert len(out) == 1
    bucket = out[0]
    assert bucket.timestamp == _BASE  # aligned to 00:00 UTC
    assert bucket.open == 100  # first
    assert bucket.close == 116  # last
    assert bucket.high == 120  # max
    assert bucket.low == 90  # min
    assert bucket.volume == 100  # sum


def test_resample_1h_to_4h_two_buckets_and_alignment() -> None:
    bars = [_bar(h, o=100 + h, h=200, low=1, c=100 + h, v=1) for h in range(8)]
    out = resample(bars, "1h", "4h")
    assert [b.timestamp for b in out] == [
        _BASE,
        _BASE + timedelta(hours=4),
    ]
    assert out[0].open == 100
    assert out[0].close == 103
    assert out[1].open == 104
    assert out[1].close == 107


def test_resample_1h_to_1d() -> None:
    bars = [_bar(h, o=100 + h, h=100 + h + 1, low=100 + h - 1, c=100 + h, v=2) for h in range(24)]
    out = resample(bars, "1h", "1d")
    assert len(out) == 1
    assert out[0].timestamp == _BASE
    assert out[0].open == 100
    assert out[0].close == 123
    assert out[0].high == 124  # 123 + 1
    assert out[0].low == 99  # 100 - 1
    assert out[0].volume == 48  # 24 * 2


def test_resample_order_independent() -> None:
    bars = [
        _bar(3, o=112, h=118, low=104, c=116, v=40),
        _bar(0, o=100, h=110, low=95, c=105, v=10),
        _bar(2, o=108, h=115, low=90, c=112, v=30),
        _bar(1, o=105, h=120, low=100, c=108, v=20),
    ]
    out = resample(bars, "1h", "4h")
    assert out[0].open == 100  # earliest timestamp, not first in list
    assert out[0].close == 116  # latest timestamp


def test_resample_finer_destination_raises() -> None:
    # Resampling to a finer timeframe is rejected (1h -> 30m).
    with pytest.raises(ValidationError) as finer:
        resample([_bar(0, 1, 2, 0.5, 1.5, 1)], "1h", "30m")
    assert finer.value.details["kind"] == "resample_finer"


def test_resample_misaligned_ratio_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whitelist timeframes all nest cleanly, so to exercise the non-integer-
    # multiple guard we inject a synthetic coarser-but-misaligned cadence
    # (7m -> 10m, ratio 10/7 is not an integer) into the cadence map the resampler
    # reads. This proves the integer-multiple contract is enforced, not just the
    # finer-than case.
    import importlib

    from trader_mcp.data.timeframes import TIMEFRAME_MS

    # ``trader_mcp.data.resample`` the *name* is shadowed by the re-exported
    # function in the package __init__, so import the submodule object explicitly.
    resample_mod = importlib.import_module("trader_mcp.data.resample")
    patched = {**TIMEFRAME_MS, "7m": 7 * 60_000, "10m": 10 * 60_000}
    monkeypatch.setattr(resample_mod, "timeframe_ms", lambda tf: patched[tf])

    with pytest.raises(ValidationError) as exc:
        resample([_bar(0, 1, 2, 0.5, 1.5, 1)], "7m", "10m")
    assert exc.value.details["kind"] == "resample_misaligned"


def test_resample_known_multiples_succeed() -> None:
    # 30m -> 1h (2x) and 5m -> 15m (3x) are clean integer multiples.
    assert len(resample([_bar(0, 1, 2, 0.5, 1.5, 1)], "30m", "1h")) == 1
    assert len(resample([_bar(0, 1, 2, 0.5, 1.5, 1)], "5m", "15m")) == 1


def test_resample_unsupported_timeframe_raises() -> None:
    with pytest.raises(ValidationError):
        resample([_bar(0, 1, 2, 0.5, 1.5, 1)], "1h", "3h")
