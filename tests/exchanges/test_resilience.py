"""Tests for the adapter's transient-retry + backoff + error-mapping wrapper.

INVARIANTS:
    * A transient fault is retried up to ``MAX_RETRIES`` with awaited backoff.
    * A success after N-1 transient failures is returned (called N times).
    * Exhausting retries raises a typed, redacted ``ExchangeError``.
    * A non-transient fault (bad symbol/auth) is NOT retried (called once).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import SECRET_TOKEN, FakeCcxt, assert_no_secret, ccxt_error
from trader_mcp.errors import ExchangeError
from trader_mcp.exchanges import ExchangeAdapter

pytestmark = pytest.mark.usefixtures("no_credentials")


async def test_transient_then_success_is_retried(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """A method that fails (transient) N-1 times then succeeds returns the success."""
    client = patch_client()
    n = adapter_module.MAX_RETRIES  # 3
    client.transient["fetch_ticker"] = (ccxt_error("network", leak=False), n - 1)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        ticker = await adapter.fetch_ticker("BTC/USDT")
        assert ticker.symbol == "BTC/USDT"
        # Called exactly N times (n-1 failures + 1 success).
        assert client.calls["fetch_ticker"] == n
        # Backoff was awaited between attempts (n-1 times).
        assert len(no_sleep) == n - 1
        assert no_sleep[0] == adapter_module.BACKOFF_BASE_SECONDS


async def test_persistent_transient_exhausts_and_raises(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """A persistently-transient fault exhausts MAX_RETRIES then raises ExchangeError."""
    client = patch_client()
    client.raise_on["fetch_ticker"] = ccxt_error("rate_limit", leak=True)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_ticker("BTC/USDT")
        assert excinfo.value.details["kind"] == "rate_limit"
        assert_no_secret(excinfo.value.message)
        assert SECRET_TOKEN not in str(excinfo.value)
        # Tried MAX_RETRIES times; backed off MAX_RETRIES-1 times.
        assert client.calls["fetch_ticker"] == adapter_module.MAX_RETRIES
        assert len(no_sleep) == adapter_module.MAX_RETRIES - 1


async def test_backoff_is_exponential(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    client = patch_client()
    client.raise_on["fetch_ticker"] = ccxt_error("network", leak=False)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        with pytest.raises(ExchangeError):
            await adapter.fetch_ticker("BTC/USDT")
    base = adapter_module.BACKOFF_BASE_SECONDS
    # delays: base * 2**0, base * 2**1, ... for MAX_RETRIES-1 backoffs.
    expected = [base * (2**i) for i in range(adapter_module.MAX_RETRIES - 1)]
    assert no_sleep == expected


async def test_non_transient_is_not_retried(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """A BadSymbol is non-transient: mapped immediately, called exactly once."""
    client = patch_client()
    client.raise_on["fetch_ticker"] = ccxt_error("bad_symbol", leak=True)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_ticker("NOPE/NOPE")
        assert excinfo.value.details["kind"] == "bad_symbol"
        assert_no_secret(excinfo.value.message)
        # No retry: exactly one call, zero backoffs.
        assert client.calls["fetch_ticker"] == 1
        assert no_sleep == []


async def test_auth_error_is_not_retried(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    client = patch_client()
    client.raise_on["fetch_order_book"] = ccxt_error("auth", leak=False)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_order_book("BTC/USDT")
        assert excinfo.value.details["kind"] == "auth"
        assert client.calls["fetch_order_book"] == 1
        assert no_sleep == []


async def test_raw_ccxt_exception_never_escapes(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """Every fetch maps failures to ExchangeError -- a raw ccxt error must not escape."""
    client = patch_client()
    client.raise_on["fetch_funding_rate"] = ccxt_error("not_supported", leak=False)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_funding_rate("BTC/USDT:USDT")
        assert excinfo.value.details["kind"] == "not_supported"
