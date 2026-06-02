"""Canonical timeframe support for the OHLCV cache.

This module is the **single source of truth** for bar cadence across the data
pipeline: gap detection, the paginated sync cursor, and multi-timeframe
resampling all derive their step size from :func:`timeframe_ms`. Scattering raw
millisecond literals anywhere else is a bug.

Only a small whitelist of CCXT timeframe keys is supported in v1; an unknown key
raises :class:`trader_mcp.errors.ValidationError` so callers never silently
compute against a wrong (or zero) cadence.
"""

from __future__ import annotations

from trader_mcp.errors import ValidationError

#: One minute in milliseconds -- the base unit every timeframe is expressed in.
_MINUTE_MS = 60_000

#: Whitelisted CCXT timeframe keys mapped to their exact duration in
#: milliseconds. Ordered shortest -> longest. This map governs everything: a key
#: absent here is unsupported. ``1w`` is exactly 7 days (no calendar drift), so it
#: is a clean integer step like the rest.
TIMEFRAME_MS: dict[str, int] = {
    "1m": 1 * _MINUTE_MS,
    "5m": 5 * _MINUTE_MS,
    "15m": 15 * _MINUTE_MS,
    "30m": 30 * _MINUTE_MS,
    "1h": 60 * _MINUTE_MS,
    "4h": 4 * 60 * _MINUTE_MS,
    "1d": 24 * 60 * _MINUTE_MS,
    "1w": 7 * 24 * 60 * _MINUTE_MS,
}

#: The supported timeframe keys, shortest -> longest (a stable, ordered view of
#: :data:`TIMEFRAME_MS`). Safe for callers to enumerate/validate against.
SUPPORTED_TIMEFRAMES: list[str] = list(TIMEFRAME_MS)


def is_supported_timeframe(timeframe: str) -> bool:
    """Return whether ``timeframe`` is a supported (whitelisted) CCXT key."""
    return timeframe in TIMEFRAME_MS


def timeframe_ms(timeframe: str) -> int:
    """Return the exact duration of one ``timeframe`` bar, in milliseconds.

    Args:
        timeframe: A CCXT timeframe key (e.g. ``"1m"``, ``"1h"``, ``"1d"``).

    Returns:
        The bar duration in milliseconds.

    Raises:
        trader_mcp.errors.ValidationError: if ``timeframe`` is not supported.
    """
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValidationError(
            f"Unsupported timeframe: {timeframe!r}. Supported: {', '.join(SUPPORTED_TIMEFRAMES)}.",
            details={
                "timeframe": timeframe,
                "kind": "unsupported_timeframe",
                "supported": SUPPORTED_TIMEFRAMES,
            },
        ) from None
