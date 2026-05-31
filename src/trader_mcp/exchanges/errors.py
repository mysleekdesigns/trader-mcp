"""Mapping CCXT exceptions to trader-mcp's typed, redacted error surface.

INVARIANT: no raw CCXT exception may escape the exchange layer. Every CCXT call is
wrapped and its failure mapped here into a :class:`trader_mcp.errors.ExchangeError`
carrying a clean, AI-friendly, **redacted** message plus structured ``details`` so
the caller (and ultimately the MCP client) can act on the failure class without
ever seeing stack traces or secret material.

The ``details["kind"]`` taxonomy is stable and tool-facing:
    ``rate_limit`` | ``bad_symbol`` | ``auth`` | ``not_supported`` |
    ``network`` | ``exchange``.
"""

from __future__ import annotations

import ccxt

from trader_mcp.config import ExchangeId
from trader_mcp.errors import ExchangeError
from trader_mcp.logging_config import redact

#: Stable error kinds surfaced in ``ExchangeError.details["kind"]``.
ErrorKind = str

# CCXT exception classes whose failures are *transient* and worth retrying with
# backoff. Exposed so the adapter's retry helper and the mapper agree on the set.
TRANSIENT_CCXT_ERRORS: tuple[type[Exception], ...] = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)


def _classify(exc: Exception) -> ErrorKind:
    """Return the stable ``kind`` for a CCXT (or unknown) exception.

    Order matters: more specific CCXT subclasses are checked before their bases.
    ``RateLimitExceeded``/``DDoSProtection`` subclass ``NetworkError`` in CCXT, so
    they must be matched before the generic network bucket.
    """
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return "rate_limit"
    if isinstance(exc, ccxt.BadSymbol):
        return "bad_symbol"
    if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied)):
        return "auth"
    if isinstance(exc, ccxt.NotSupported):
        return "not_supported"
    if isinstance(exc, (ccxt.RequestTimeout, ccxt.ExchangeNotAvailable, ccxt.NetworkError)):
        return "network"
    # Any other ccxt.BaseError, or a non-CCXT Exception, is a generic exchange fault.
    return "exchange"


#: Short, AI-friendly hints keyed by error kind (no secrets, no raw CCXT text).
_KIND_MESSAGES: dict[ErrorKind, str] = {
    "rate_limit": "Rate limited by the exchange; back off and retry later.",
    "bad_symbol": "The requested symbol is not valid for this exchange/market type.",
    "auth": "Authentication failed; check that the exchange API credentials are valid.",
    "not_supported": "This operation is not supported by the exchange.",
    "network": "A network or availability error occurred talking to the exchange.",
    "exchange": "The exchange operation failed.",
}


def map_ccxt_error(exc: Exception, *, exchange: ExchangeId, op: str) -> ExchangeError:
    """Map a CCXT (or unexpected) exception to a typed, redacted ``ExchangeError``.

    The returned error's ``message`` is a stable, AI-friendly summary; the raw CCXT
    message (which may embed a URL, request body, or -- in an auth error -- key
    material) is captured only after passing through :func:`redact`, and is placed
    in ``details["cause"]`` for diagnostics.

    Args:
        exc: The exception raised by a CCXT call.
        exchange: The exchange the failing op ran against.
        op: The adapter operation name (e.g. ``"fetch_ohlcv"``).

    Returns:
        An :class:`ExchangeError` with a redacted message and structured
        ``details`` containing at least ``kind``, ``exchange``, and ``op``.
    """
    kind = _classify(exc)
    summary = _KIND_MESSAGES.get(kind, _KIND_MESSAGES["exchange"])
    # Defense-in-depth: redact the raw cause even though ExchangeError messages are
    # redacted again at the MCP boundary. Never let key material reach details.
    cause = redact(str(exc))
    message = redact(f"{exchange}.{op}: {summary}")
    return ExchangeError(
        message,
        details={
            "kind": kind,
            "exchange": exchange,
            "op": op,
            "cause": cause,
        },
    )
