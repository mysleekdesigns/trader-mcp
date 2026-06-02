"""Tests for CCXT-exception -> typed, redacted ``ExchangeError`` mapping.

INVARIANT under test: no raw CCXT exception escapes the exchange layer, every
failure maps to a stable ``details["kind"]``, and the secret-shaped token never
survives into the mapped message/details.
"""

from __future__ import annotations

import ccxt
import pytest

from tests._fakes import (
    ERROR_KIND_CASES,
    SECRET_TOKEN,
    assert_no_secret,
    ccxt_error,
)
from trader_mcp.errors import ExchangeError
from trader_mcp.exchanges import map_ccxt_error
from trader_mcp.exchanges.errors import TRANSIENT_CCXT_ERRORS


@pytest.mark.parametrize(("kind", "expected"), ERROR_KIND_CASES)
def test_map_ccxt_error_classifies_kind(kind: str, expected: str) -> None:
    exc = ccxt_error(kind)
    mapped = map_ccxt_error(exc, exchange="coinbase", op="fetch_ticker")
    assert isinstance(mapped, ExchangeError)
    assert mapped.details["kind"] == expected
    assert mapped.details["exchange"] == "coinbase"
    assert mapped.details["op"] == "fetch_ticker"


@pytest.mark.parametrize(("kind", "_expected"), ERROR_KIND_CASES)
def test_map_ccxt_error_redacts_message_and_details(kind: str, _expected: str) -> None:
    """The secret-shaped token must not survive into message or details."""
    exc = ccxt_error(kind, leak=True)
    mapped = map_ccxt_error(exc, exchange="coinbase", op="fetch_balance")

    assert_no_secret(mapped.message)
    assert_no_secret(mapped.details["cause"])
    # The raw cause is captured (redacted) for diagnostics, not dropped entirely.
    assert "REDACTED" in mapped.details["cause"]
    # Sanity: the plaintext token genuinely existed before mapping.
    assert SECRET_TOKEN in str(exc)


def test_rate_limit_takes_priority_over_network() -> None:
    """RateLimitExceeded subclasses NetworkError in CCXT; it must classify as rate_limit."""
    assert issubclass(ccxt.RateLimitExceeded, ccxt.NetworkError)
    mapped = map_ccxt_error(
        ccxt.RateLimitExceeded("slow down"), exchange="coinbase", op="fetch_ohlcv"
    )
    assert mapped.details["kind"] == "rate_limit"


def test_permission_denied_maps_to_auth() -> None:
    mapped = map_ccxt_error(ccxt.PermissionDenied("no"), exchange="coinbase", op="x")
    assert mapped.details["kind"] == "auth"


def test_non_ccxt_exception_maps_to_generic_exchange() -> None:
    mapped = map_ccxt_error(RuntimeError("boom"), exchange="coinbase", op="x")
    assert mapped.details["kind"] == "exchange"


def test_transient_set_matches_retry_contract() -> None:
    """The transient set the adapter retries on must include the documented faults."""
    assert ccxt.NetworkError in TRANSIENT_CCXT_ERRORS
    assert ccxt.RateLimitExceeded in TRANSIENT_CCXT_ERRORS
    # Non-transient faults must NOT be in the retry set.
    assert ccxt.BadSymbol not in TRANSIENT_CCXT_ERRORS
    assert ccxt.AuthenticationError not in TRANSIENT_CCXT_ERRORS
    assert ccxt.NotSupported not in TRANSIENT_CCXT_ERRORS
