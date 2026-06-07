"""Tests for the adapter's resilience layer: transient-retry + backoff +
error-mapping (REST), exchange-downtime handling, and local-vs-exchange clock skew.

INVARIANTS:
    * A transient fault is retried up to ``MAX_RETRIES`` with awaited backoff.
    * A success after N-1 transient failures is returned (called N times).
    * Exhausting retries raises a typed, redacted ``ExchangeError``.
    * A non-transient fault (bad symbol/auth) is NOT retried (called once).
    * Maintenance / availability faults are treated as transient (retried); a
      bad-symbol/auth fault is non-transient (mapped immediately).
    * Clock-skew detection compares the local clock to the exchange server time,
      flags drift past a threshold, and never leaks secret material.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import ccxt
import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FIXED_MS, SECRET_TOKEN, FakeCcxt, assert_no_secret, ccxt_error
from trader_mcp.errors import ExchangeError
from trader_mcp.exchanges import ExchangeAdapter
from trader_mcp.exchanges.models import ClockSkew

pytestmark = pytest.mark.usefixtures("no_credentials")


async def test_transient_then_success_is_retried(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """A method that fails (transient) N-1 times then succeeds returns the success."""
    client = patch_client()
    n = adapter_module.MAX_RETRIES  # 3
    client.transient["fetch_ticker"] = (ccxt_error("network", leak=False), n - 1)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        ticker = await adapter.fetch_ticker("BTC/USD")
        assert ticker.symbol == "BTC/USD"
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
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_ticker("BTC/USD")
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
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError):
            await adapter.fetch_ticker("BTC/USD")
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
    adapter = await ExchangeAdapter.create("coinbase")
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
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_order_book("BTC/USD")
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
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_funding_rate("BTC/USD:USD")
        assert excinfo.value.details["kind"] == "not_supported"


# --------------------------------------------------------------------------- #
# Exchange-downtime: maintenance / availability faults are transient
# --------------------------------------------------------------------------- #
async def test_maintenance_is_transient_and_retried(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """A scheduled-maintenance (OnMaintenance) fault is transient: retried, recovers.

    OnMaintenance subclasses ExchangeNotAvailable; a maintenance window must NOT fail
    the call outright -- the adapter backs off and recovers when the exchange returns.
    """
    client = patch_client()
    n = adapter_module.MAX_RETRIES
    client.transient["fetch_ticker"] = (ccxt.OnMaintenance("under maintenance"), n - 1)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        ticker = await adapter.fetch_ticker("BTC/USD")
    assert ticker.symbol == "BTC/USD"
    assert client.calls["fetch_ticker"] == n
    assert len(no_sleep) == n - 1


async def test_exchange_not_available_exhausts_to_typed_error(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
) -> None:
    """Persistent downtime (ExchangeNotAvailable) exhausts retries -> typed, redacted error."""
    client = patch_client()
    client.raise_on["fetch_ticker"] = ccxt.ExchangeNotAvailable(f"503 maintenance {SECRET_TOKEN}")
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.fetch_ticker("BTC/USD")
    assert excinfo.value.details["kind"] == "network"
    assert_no_secret(excinfo.value.message)
    assert SECRET_TOKEN not in str(excinfo.value)
    assert client.calls["fetch_ticker"] == adapter_module.MAX_RETRIES


# --------------------------------------------------------------------------- #
# Clock-skew detection
# --------------------------------------------------------------------------- #
@pytest.fixture
def fixed_local_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Patch the adapter's ``_now_ms`` to a fixed local time (ms). Returns a setter."""

    def set_now(ms: float) -> None:
        monkeypatch.setattr(adapter_module, "_now_ms", lambda: ms)

    return set_now


async def test_clock_skew_in_tolerance(
    patch_client: Callable[..., FakeCcxt],
    fixed_local_clock: Callable[[float], None],
) -> None:
    """Local clock matching server time is within tolerance, no warning state."""
    client = patch_client()
    client.server_time_ms = FIXED_MS
    fixed_local_clock(float(FIXED_MS))  # local == server
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        skew = await adapter.check_clock_skew()
    assert isinstance(skew, ClockSkew)
    assert skew.skew_ms == 0.0
    assert skew.within_tolerance is True
    assert skew.threshold_ms == adapter_module.CLOCK_SKEW_WARN_MS
    assert client.calls["fetch_time"] == 1


async def test_clock_skew_local_ahead_out_of_tolerance(
    patch_client: Callable[..., FakeCcxt],
    fixed_local_clock: Callable[[float], None],
) -> None:
    """A local clock well ahead of the exchange is flagged out of tolerance (positive)."""
    client = patch_client()
    client.server_time_ms = FIXED_MS
    fixed_local_clock(float(FIXED_MS + 5000))  # local 5s ahead
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        skew = await adapter.check_clock_skew(threshold_ms=1000.0)
    assert skew.skew_ms == 5000.0  # positive: local ahead of exchange
    assert skew.within_tolerance is False


async def test_clock_skew_local_behind_is_negative(
    patch_client: Callable[..., FakeCcxt],
    fixed_local_clock: Callable[[float], None],
) -> None:
    """A local clock behind the exchange yields a negative skew."""
    client = patch_client()
    client.server_time_ms = FIXED_MS
    fixed_local_clock(float(FIXED_MS - 3000))  # local 3s behind
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        skew = await adapter.check_clock_skew(threshold_ms=1000.0)
    assert skew.skew_ms == -3000.0
    assert skew.within_tolerance is False


async def test_clock_skew_warns_redacted(
    patch_client: Callable[..., FakeCcxt],
    fixed_local_clock: Callable[[float], None],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-tolerance skew logs a warning that carries no secret material.

    The package logger has ``propagate=False`` (so its redacting stderr handler is
    the sole sink in production); we enable propagation for this test so caplog's
    root handler also sees the record and we can assert on its content.
    """
    pkg_logger = logging.getLogger("trader_mcp")
    monkeypatch.setattr(pkg_logger, "propagate", True)

    client = patch_client()
    client.server_time_ms = FIXED_MS
    fixed_local_clock(float(FIXED_MS + 9000))
    adapter = await ExchangeAdapter.create("coinbase")
    with caplog.at_level("WARNING", logger="trader_mcp"):
        async with adapter:
            await adapter.check_clock_skew(threshold_ms=500.0)
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("clock skew" in m for m in messages)
    for m in messages:
        assert_no_secret(m)


async def test_clock_skew_uses_milliseconds_fallback(
    patch_client: Callable[..., FakeCcxt],
    fixed_local_clock: Callable[[float], None],
) -> None:
    """When fetch_time is absent, the synchronous milliseconds() fallback is used."""
    client = patch_client()
    # Remove fetch_time; expose a synchronous milliseconds() like real CCXT clients.
    object.__setattr__(client, "fetch_time", None)
    client.milliseconds = lambda: FIXED_MS  # type: ignore[attr-defined]
    fixed_local_clock(float(FIXED_MS))
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        skew = await adapter.check_clock_skew()
    assert skew.skew_ms == 0.0
    assert skew.within_tolerance is True


async def test_clock_skew_no_server_time_method_is_typed(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    """A client exposing neither fetch_time nor milliseconds raises a typed error."""
    client = patch_client()
    object.__setattr__(client, "fetch_time", None)
    object.__setattr__(client, "milliseconds", None)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.check_clock_skew()
    assert excinfo.value.details["kind"] == "not_supported"


async def test_clock_skew_server_time_error_is_mapped(
    patch_client: Callable[..., FakeCcxt],
    no_sleep: list[float],
    fixed_local_clock: Callable[[float], None],
) -> None:
    """A failing server-time read is mapped to a typed, redacted ExchangeError."""
    client = patch_client()
    fixed_local_clock(float(FIXED_MS))
    client.raise_on["fetch_time"] = ccxt_error("auth", leak=True)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.check_clock_skew()
    assert excinfo.value.details["kind"] == "auth"
    assert_no_secret(excinfo.value.message)
