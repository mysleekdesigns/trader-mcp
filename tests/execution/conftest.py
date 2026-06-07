"""Shared offline builders for the execution-runtime tests (deterministic, no net)."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Sequence
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
    open_from_prev: bool = True,
) -> list[OHLCVBar]:
    """Build OHLCV bars from closes (open == prev close by default for gapless fills)."""
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


class ListFeed:
    """An async bar feed backed by an in-memory list (the injected BarFeed)."""

    def __init__(self, bars: Sequence[OHLCVBar]) -> None:
        self._bars = list(bars)

    async def __aiter__(self) -> AsyncIterator[OHLCVBar]:
        for bar in self._bars:
            yield bar


@pytest.fixture
def sine_bars() -> list[OHLCVBar]:
    """A 400-bar sine wave (mean 100, amplitude 10) -- repeated MA/RSI crossings."""
    closes = [100 + 10 * math.sin(i / 10.0) for i in range(400)]
    return make_bars(closes)
