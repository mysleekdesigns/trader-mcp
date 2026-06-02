"""Pure data-quality functions over OHLCV bars.

Stateless helpers that the store and the sync pipeline compose: normalize (sort +
dedupe + tz-normalize), gap detection, and a monotonic check. All cadence math
goes through :func:`trader_mcp.data.timeframes.timeframe_ms` so there is a single
source of truth for bar spacing.

Conventions:
    * Bars are :class:`trader_mcp.exchanges.models.OHLCVBar` with tz-aware UTC
      ``timestamp`` fields. The adapter already produces UTC, but these functions
      defensively coerce naive datetimes to UTC so they are safe on any input.
    * "Normalized" means: ascending by timestamp, no duplicate timestamps, UTC.
"""

from __future__ import annotations

from datetime import UTC
from itertools import pairwise

from trader_mcp.data.models import Gap
from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.exchanges.models import OHLCVBar, ms_to_datetime


def _to_utc(bar: OHLCVBar) -> OHLCVBar:
    """Return ``bar`` with a tz-aware UTC timestamp (no-op if already UTC)."""
    ts = bar.timestamp
    if ts.tzinfo is None:
        return bar.model_copy(update={"timestamp": ts.replace(tzinfo=UTC)})
    if ts.utcoffset() != UTC.utcoffset(None):
        return bar.model_copy(update={"timestamp": ts.astimezone(UTC)})
    return bar


def normalize(bars: list[OHLCVBar], timeframe: str) -> tuple[list[OHLCVBar], int]:
    """Sort ascending, drop duplicate timestamps, and normalize to UTC.

    On a duplicate timestamp the **last** bar seen for that timestamp wins (a
    re-fetch of the same candle supersedes the earlier copy). The cleaned series
    is returned together with the number of duplicate bars removed.

    Args:
        bars: Raw bars in any order, possibly with duplicate timestamps.
        timeframe: The dataset cadence (validated; raises on an unknown key).

    Returns:
        ``(cleaned_bars, duplicates_removed)`` -- ascending, deduped, UTC.
    """
    # Validate the timeframe even though normalize() does not need the step, so a
    # bad cadence fails fast at the same boundary as gap detection.
    timeframe_ms(timeframe)

    by_ts: dict[int, OHLCVBar] = {}
    duplicates = 0
    for raw in bars:
        bar = _to_utc(raw)
        key = int(bar.timestamp.timestamp() * 1000)
        if key in by_ts:
            duplicates += 1
        by_ts[key] = bar
    cleaned = [by_ts[k] for k in sorted(by_ts)]
    return cleaned, duplicates


def is_monotonic(bars: list[OHLCVBar]) -> bool:
    """Return whether timestamps are strictly increasing (no equal/out-of-order)."""
    return all(cur.timestamp > prev.timestamp for prev, cur in pairwise(bars))


def detect_gaps(bars: list[OHLCVBar], timeframe: str) -> list[Gap]:
    """Find every contiguous run of missing bars between first and last timestamp.

    Operates on the bars as given; callers should pass a :func:`normalize`-d
    series (ascending, deduped) so the cadence comparison is meaningful. A "gap"
    is any stretch where consecutive cached timestamps differ by more than one
    bar step; each returned :class:`Gap` covers the *missing* bars (its ``start``
    is the first absent bar's open time, ``end`` the last absent bar's).

    Args:
        bars: Normalized bars (ascending, deduped, UTC).
        timeframe: The dataset cadence.

    Returns:
        Gaps in ascending order (empty when the series is dense or has < 2 bars).
    """
    step = timeframe_ms(timeframe)
    gaps: list[Gap] = []
    for prev, cur in pairwise(bars):
        prev_ms = int(prev.timestamp.timestamp() * 1000)
        cur_ms = int(cur.timestamp.timestamp() * 1000)
        delta = cur_ms - prev_ms
        # A dense pair differs by exactly one step. Anything larger leaves
        # (delta/step - 1) missing bars between them. Ignore non-positive deltas
        # (handled by is_monotonic / dedupe upstream).
        if delta <= step:
            continue
        missing = delta // step - 1
        if missing <= 0:
            continue

        first_missing = ms_to_datetime(prev_ms + step)
        last_missing = ms_to_datetime(cur_ms - step)
        # ms_to_datetime only returns None for a None input; these are real ints.
        assert first_missing is not None
        assert last_missing is not None
        gaps.append(Gap(start=first_missing, end=last_missing, missing_bars=missing))
    return gaps


def expected_bar_count(bars: list[OHLCVBar], timeframe: str) -> int:
    """Return how many bars a dense series would hold from first to last timestamp.

    ``0`` for an empty series, ``1`` for a single bar. Used to compute coverage in
    :class:`trader_mcp.data.models.DatasetInspection`.
    """
    if not bars:
        return 0
    step = timeframe_ms(timeframe)
    first_ms = int(bars[0].timestamp.timestamp() * 1000)
    last_ms = int(bars[-1].timestamp.timestamp() * 1000)
    return (last_ms - first_ms) // step + 1
