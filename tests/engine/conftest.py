"""Shared bar/spec builders for the engine self-tests (deterministic, offline)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from trader_mcp.exchanges.models import OHLCVBar


def make_bars(
    closes: Sequence[float],
    *,
    start: datetime | None = None,
    step_hours: int = 1,
    high_mult: float = 1.0,
    low_mult: float = 1.0,
    open_from_prev: bool = False,
) -> list[OHLCVBar]:
    """Build OHLCV bars from a list of closes (open == close unless overridden).

    ``open_from_prev`` opens each bar at the previous bar's close (a realistic
    gapless series) -- useful for exercising the next-bar-open fill convention.
    ``high_mult``/``low_mult`` widen the bar range for SL/TP intrabar tests.
    """
    t0 = start or datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    for i, close in enumerate(closes):
        open_px = closes[i - 1] if (open_from_prev and i > 0) else close
        hi = max(open_px, close) * high_mult
        lo = min(open_px, close) * low_mult
        bars.append(
            OHLCVBar(
                timestamp=t0 + timedelta(hours=step_hours * i),
                open=open_px,
                high=hi,
                low=lo,
                close=close,
                volume=1.0,
            )
        )
    return bars


@pytest.fixture
def sine_bars() -> list[OHLCVBar]:
    """A 400-bar sine wave (mean 100, amplitude 10) -- repeated MA/RSI crossings."""
    closes = [100 + 10 * math.sin(i / 10.0) for i in range(400)]
    return make_bars(closes, open_from_prev=True)
