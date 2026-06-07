"""Edge-case coverage for the adapter: testnet wiring, market normalization
fallbacks, and malformed-row handling that the happy-path tests don't reach.

All offline, no credentials.
"""

from __future__ import annotations

from collections.abc import Callable

import ccxt
import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FIXED_MS, FakeCcxt
from trader_mcp.errors import ExchangeError
from trader_mcp.exchanges import ExchangeAdapter
from trader_mcp.exchanges.adapter import _to_float

pytestmark = pytest.mark.usefixtures("no_credentials")


# --------------------------------------------------------------------------- #
# _to_float coercion
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("not-a-number", None),
        ("12.5", 12.5),
        (7, 7.0),
        (3.0, 3.0),
    ],
)
def test_to_float_coercion(value: object, expected: float | None) -> None:
    assert _to_float(value) == expected


# --------------------------------------------------------------------------- #
# testnet / sandbox wiring
# --------------------------------------------------------------------------- #
async def test_testnet_calls_set_sandbox_mode(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    client = patch_client()
    adapter = await ExchangeAdapter.create("coinbase", testnet=True)
    async with adapter:
        assert adapter.testnet is True
        assert client.sandbox_calls == [True]


async def test_testnet_notsupported_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If set_sandbox_mode raises NotSupported, create() refuses (typed error).

    Phase 5 contract: we never silently fall through to live endpoints when a
    requested sandbox cannot be enabled -- that would risk a real-money order.
    """
    client = FakeCcxt()

    def boom(enabled: bool) -> None:
        raise ccxt.NotSupported("no sandbox here")

    monkeypatch.setattr(client, "set_sandbox_mode", boom)
    monkeypatch.setattr(
        adapter_module,
        "_create_ccxt_client",
        lambda eid, config: client,
    )
    with pytest.raises(ExchangeError) as excinfo:
        await ExchangeAdapter.create("coinbase", testnet=True)
    assert excinfo.value.details["kind"] == "not_supported"


async def test_no_testnet_does_not_touch_sandbox(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    client = patch_client()
    adapter = await ExchangeAdapter.create("coinbase", testnet=False)
    async with adapter:
        assert client.sandbox_calls == []


async def test_testnet_client_without_sandbox_method_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client whose set_sandbox_mode is not callable refuses sandbox (typed error)."""
    client = FakeCcxt()
    # Shadow the class method with a non-callable so the adapter's
    # ``callable(...)`` guard rejects sandbox rather than silently using live.
    monkeypatch.setattr(client, "set_sandbox_mode", None)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", lambda eid, config: client)
    with pytest.raises(ExchangeError) as excinfo:
        await ExchangeAdapter.create("coinbase", testnet=True)
    assert excinfo.value.details["kind"] == "not_supported"


# --------------------------------------------------------------------------- #
# aclose lifecycle
# --------------------------------------------------------------------------- #
async def test_aclose_is_idempotent(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    client = patch_client()
    adapter = await ExchangeAdapter.create("coinbase")
    await adapter.aclose()
    assert client.closed is True
    # Second close is a no-op (does not re-await client.close()).
    await adapter.aclose()


async def test_aclose_swallows_client_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing client.close() is logged (redacted) and swallowed, not raised."""
    client = FakeCcxt()

    async def boom() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(client, "close", boom)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", lambda eid, config: client)
    adapter = await ExchangeAdapter.create("coinbase")
    # Must not raise despite the underlying close() error.
    await adapter.aclose()


# --------------------------------------------------------------------------- #
# market normalization fallbacks
# --------------------------------------------------------------------------- #
async def test_market_type_fallback_to_raw_type(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    """A market with no spot/swap flags but type='swap' is still classified."""
    raw = {
        "ALT/USD:USD": {
            "symbol": "ALT/USD:USD",
            "base": "ALT",
            "quote": "USD",
            "settle": "USD",
            "type": "swap",  # only the raw type signals it
            "active": True,
            "precision": {},
            "limits": {},
        }
    }
    patch_client(markets=raw)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        markets = await adapter.load_markets()
        assert len(markets) == 1
        assert markets[0].type == "swap"


async def test_market_missing_symbol_is_dropped(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    """A spot market missing base/quote is dropped (cannot be normalized)."""
    raw = {
        "BAD": {"symbol": "BAD", "spot": True, "type": "spot", "active": True},
        "BTC/USD": {
            "symbol": "BTC/USD",
            "base": "BTC",
            "quote": "USD",
            "spot": True,
            "type": "spot",
            "active": True,
            "precision": {},
            "limits": {},
        },
    }
    patch_client(markets=raw)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        markets = await adapter.load_markets()
        # Only the well-formed market survives.
        assert [m.symbol for m in markets] == ["BTC/USD"]


async def test_ohlcv_skips_row_with_null_timestamp(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    """A 6-col OHLCV row with a None timestamp is skipped (not raised)."""
    client = patch_client()
    client.ohlcv_data = [
        [None, 1.0, 2.0, 0.5, 1.5, 10.0],  # null ts -> skipped
        [1_704_164_645_000, 1.0, 2.0, 0.5, 1.5, 10.0],  # kept
    ]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        result = await adapter.fetch_ohlcv("BTC/USD", "1h")
        assert result.count == 1


async def test_ohlcv_skips_row_with_malformed_body_cell(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    """A 6-col row with a None/non-numeric BODY cell is skipped, not raised.

    Exchanges emit ``None`` close/volume on thin or just-opened candles; a bare
    ``float()`` on such a cell would raise OUTSIDE ``_call`` and let a raw
    exception escape the adapter. The hardened path coerces every body cell via
    ``_to_float`` and drops the bar when any cell is missing/non-numeric.
    """
    client = patch_client()
    client.ohlcv_data = [
        [FIXED_MS, None, 2.0, 0.5, 1.5, 10.0],  # None open -> skipped
        [FIXED_MS + 3_600_000, 1.0, "x", 0.5, 1.5, 10.0],  # non-numeric high -> skipped
        [FIXED_MS + 7_200_000, 1.0, 2.0, 0.5, 1.5, None],  # None volume -> skipped
        [FIXED_MS + 10_800_000, 1.0, 2.0, 0.5, 1.5, 10.0],  # all good -> kept
    ]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        result = await adapter.fetch_ohlcv("BTC/USD", "1h")
        # Only the well-formed bar survives; the three malformed rows are dropped.
        assert result.count == 1
        assert len(result.bars) == 1
        bar = result.bars[0]
        assert bar.open == 1.0
        assert bar.high == 2.0
        assert bar.low == 0.5
        assert bar.close == 1.5
        assert bar.volume == 10.0


async def test_order_book_skips_level_with_non_numeric_price(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    """An order-book level whose price won't coerce to float is skipped."""
    client = patch_client()
    client.order_book_data = {
        "symbol": "BTC/USD",
        "timestamp": 1_704_164_645_000,
        "bids": [["not-a-price", 1.0], [42000.0, 2.0]],
        "asks": [[42001.0, 1.0]],
    }
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        book = await adapter.fetch_order_book("BTC/USD")
        # The malformed bid is dropped; one good bid remains.
        assert [b.price for b in book.bids] == [42000.0]
        assert len(book.asks) == 1


async def test_load_markets_falls_back_to_client_markets_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If load_markets returns a non-dict, the adapter reads client.markets."""
    client = FakeCcxt()

    async def weird_load(reload: bool = False) -> None:
        client.markets = {
            "BTC/USD": {
                "symbol": "BTC/USD",
                "base": "BTC",
                "quote": "USD",
                "spot": True,
                "type": "spot",
                "active": True,
                "precision": {},
                "limits": {},
            }
        }
        return None  # non-dict return forces the .markets fallback branch

    monkeypatch.setattr(client, "load_markets", weird_load)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", lambda eid, config: client)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        markets = await adapter.load_markets()
        assert [m.symbol for m in markets] == ["BTC/USD"]
