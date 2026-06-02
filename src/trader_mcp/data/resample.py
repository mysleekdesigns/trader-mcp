"""Multi-timeframe OHLCV resampling.

Aggregates a series of source bars into a coarser destination timeframe with
standard OHLCV semantics:

    * open   = first bar's open in the bucket
    * high   = max high in the bucket
    * low    = min low in the bucket
    * close  = last bar's close in the bucket
    * volume = sum of volumes in the bucket

Buckets are aligned to the UTC epoch (bucket index = ``floor(ts_ms / dst_ms)``),
so a 4h bucket starts at 00:00/04:00/... UTC, matching how exchanges label
higher-timeframe candles. The destination timeframe must be an exact integer
multiple of the source; otherwise a :class:`trader_mcp.errors.ValidationError` is
raised (a misaligned ratio cannot produce well-defined buckets).

Pure Python by design (no DuckDB needed): the bucketing is a single linear pass,
and keeping it dependency-free makes it trivially usable from the in-memory
backtest path.
"""

from __future__ import annotations

from dataclasses import dataclass

from trader_mcp.data.timeframes import timeframe_ms
from trader_mcp.errors import ValidationError
from trader_mcp.exchanges.models import OHLCVBar, ms_to_datetime


@dataclass
class _Bucket:
    """Mutable accumulator for one destination bucket.

    ``open``/``close`` are chosen by the earliest/latest *timestamp* in the bucket
    (not list position), so resampling is correct regardless of input order.
    """

    open_ts: int
    open: float
    close_ts: int
    close: float
    high: float
    low: float
    volume: float

    def add(self, ts_ms: int, bar: OHLCVBar) -> None:
        if ts_ms < self.open_ts:
            self.open_ts = ts_ms
            self.open = bar.open
        if ts_ms > self.close_ts:
            self.close_ts = ts_ms
            self.close = bar.close
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.volume += bar.volume


def resample(bars: list[OHLCVBar], src_tf: str, dst_tf: str) -> list[OHLCVBar]:
    """Resample ``bars`` from ``src_tf`` to the coarser ``dst_tf``.

    Args:
        bars: Source bars (any order; bucketing is by timestamp, so list order
            within a bucket does not affect open/close -- those are chosen by time).
        src_tf: Source timeframe key (e.g. ``"1h"``).
        dst_tf: Destination timeframe key (e.g. ``"4h"``); must be an integer
            multiple of ``src_tf`` and not finer than it.

    Returns:
        Resampled bars in ascending timestamp order, each stamped at its bucket's
        UTC start. An empty input yields an empty list.

    Raises:
        trader_mcp.errors.ValidationError: if either timeframe is unsupported, if
            ``dst_tf`` is finer than ``src_tf``, or if ``dst_tf`` is not an exact
            integer multiple of ``src_tf``.
    """
    src_ms = timeframe_ms(src_tf)
    dst_ms = timeframe_ms(dst_tf)

    if dst_ms < src_ms:
        raise ValidationError(
            f"Cannot resample to a finer timeframe: {src_tf!r} -> {dst_tf!r}.",
            details={"src_tf": src_tf, "dst_tf": dst_tf, "kind": "resample_finer"},
        )
    if dst_ms % src_ms != 0:
        raise ValidationError(
            f"Destination timeframe {dst_tf!r} is not an integer multiple of "
            f"source {src_tf!r} ({dst_ms} % {src_ms} != 0).",
            details={"src_tf": src_tf, "dst_tf": dst_tf, "kind": "resample_misaligned"},
        )

    if not bars:
        return []

    buckets: dict[int, _Bucket] = {}
    for bar in bars:
        ts_ms = int(bar.timestamp.timestamp() * 1000)
        bucket_ms = (ts_ms // dst_ms) * dst_ms
        existing = buckets.get(bucket_ms)
        if existing is None:
            buckets[bucket_ms] = _Bucket(
                open_ts=ts_ms,
                open=bar.open,
                close_ts=ts_ms,
                close=bar.close,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
            )
        else:
            existing.add(ts_ms, bar)

    out: list[OHLCVBar] = []
    for bucket_ms in sorted(buckets):
        agg = buckets[bucket_ms]
        ts = ms_to_datetime(bucket_ms)
        assert ts is not None
        out.append(
            OHLCVBar(
                timestamp=ts,
                open=agg.open,
                high=agg.high,
                low=agg.low,
                close=agg.close,
                volume=agg.volume,
            )
        )
    return out
