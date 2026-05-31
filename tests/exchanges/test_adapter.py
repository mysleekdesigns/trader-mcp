"""Tests for :class:`ExchangeAdapter` against a fake CCXT client (fully offline).

Covers capabilities derivation, market normalization (drops unsupported types),
list/search filters, and the typed normalization of every market-data call,
including ms->UTC datetime conversion and skipping malformed rows.

The exchange layer is read-only: this module also asserts the adapter exposes NO
order-placement surface (safe-by-default invariant).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from tests._fakes import FIXED_MS, FakeCcxt
from trader_mcp.exchanges import (
    ExchangeAdapter,
    ExchangeCapabilities,
    FundingRate,
    Market,
    OHLCVResult,
    OrderBook,
    RecentTradesResult,
    Ticker,
)

# Every adapter test runs fully offline + with no real credentials.
pytestmark = pytest.mark.usefixtures("no_credentials")

#: The fixed payload timestamp as a tz-aware UTC datetime (expected value).
FIXED_DT = datetime.fromtimestamp(FIXED_MS / 1000, tz=UTC)


async def _adapter(patch_client: Callable[..., FakeCcxt], **kwargs: object) -> ExchangeAdapter:
    """Build a bybit adapter wrapping a freshly-installed fake client."""
    patch_client(**kwargs)
    return await ExchangeAdapter.create("bybit")


# --------------------------------------------------------------------------- #
# Read-only / safe-by-default surface
# --------------------------------------------------------------------------- #
def test_adapter_exposes_no_order_placement_surface() -> None:
    """The read-only invariant: no order/trade-routing methods exist on the adapter."""
    forbidden = (
        "create_order",
        "place_order",
        "cancel_order",
        "edit_order",
        "create_market_order",
        "create_limit_order",
        "transfer",
        "withdraw",
    )
    for name in forbidden:
        assert not hasattr(ExchangeAdapter, name), f"adapter must not expose {name}"


# --------------------------------------------------------------------------- #
# capabilities() -- no network
# --------------------------------------------------------------------------- #
async def test_capabilities_maps_has_and_timeframes(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        caps = adapter.capabilities()
        assert isinstance(caps, ExchangeCapabilities)
        assert caps.exchange == "bybit"
        assert caps.ccxt_id == "bybit"
        assert caps.supports_ohlcv is True
        assert caps.supports_order_book is True
        assert caps.supports_trades is True
        assert caps.supports_ticker is True
        # "emulated" must be treated as supported.
        assert caps.supports_funding_rate is True
        assert caps.timeframes == ["1m", "1h", "1d"]
        # has_websocket comes from the registry, not from the client.
        assert caps.has_websocket is True


async def test_capabilities_supports_testnet_from_urls_test(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client, urls={"test": "https://testnet.example"})
    async with adapter:
        assert adapter.capabilities().supports_testnet is True


async def test_capabilities_no_testnet_when_urls_test_absent(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client, urls={"api": "https://live.example"})
    async with adapter:
        assert adapter.capabilities().supports_testnet is False


async def test_capabilities_market_count_none_before_load_set_after(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        assert adapter.capabilities().market_count is None
        await adapter.load_markets()
        # 6 raw markets, but capabilities reflects the client's full markets dict.
        assert adapter.capabilities().market_count == 6


# --------------------------------------------------------------------------- #
# markets: normalization + filters
# --------------------------------------------------------------------------- #
async def test_load_markets_normalizes_spot_and_swap_drops_unsupported(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        markets = await adapter.load_markets()
        # 5 supported (3 spot + 2 swap); the option market is dropped.
        assert all(isinstance(m, Market) for m in markets)
        symbols = {m.symbol for m in markets}
        assert "BTC/USDT" in symbols
        assert "ETH/USDT:USDT" in symbols
        assert not any("251226" in s for s in symbols)  # option dropped
        assert len(markets) == 5


async def test_normalized_market_fields(patch_client: Callable[..., FakeCcxt]) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        markets = {m.symbol: m for m in await adapter.load_markets()}
        spot = markets["BTC/USDT"]
        assert spot.type == "spot"
        assert spot.base == "BTC"
        assert spot.quote == "USDT"
        assert spot.price_precision == 0.01
        assert spot.amount_precision == 0.0001
        assert spot.min_amount == 0.0001
        assert spot.max_amount == 1000.0
        assert spot.min_cost == 5.0
        assert spot.maker_fee == 0.001
        assert spot.taker_fee == 0.0015

        swap = markets["ETH/USDT:USDT"]
        assert swap.type == "swap"
        assert swap.settle == "USDT"
        assert swap.linear is True
        assert swap.contract_size == 1.0


async def test_list_markets_active_only_filter(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        active = await adapter.list_markets(active_only=True)
        assert all(m.active for m in active)
        # SOL/BTC is inactive in the fixtures -> dropped when active_only.
        assert "SOL/BTC" not in {m.symbol for m in active}

        everything = await adapter.list_markets(active_only=False)
        assert "SOL/BTC" in {m.symbol for m in everything}


async def test_list_markets_type_filter(patch_client: Callable[..., FakeCcxt]) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        spots = await adapter.list_markets(market_type="spot")
        assert {m.type for m in spots} == {"spot"}
        swaps = await adapter.list_markets(market_type="swap")
        assert {m.type for m in swaps} == {"swap"}


async def test_list_markets_limit(patch_client: Callable[..., FakeCcxt]) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        limited = await adapter.list_markets(limit=2)
        assert len(limited) == 2


# --------------------------------------------------------------------------- #
# search_symbols
# --------------------------------------------------------------------------- #
async def test_search_symbols_case_insensitive_substring(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        # Matches on base "BTC" across spot+swap, case-insensitively.
        matches = await adapter.search_symbols("btc")
        symbols = {m.symbol for m in matches}
        assert "BTC/USDT" in symbols
        assert "BTC/USDT:USDT" in symbols
        # No ETH-only market should appear for a BTC query.
        assert "ETH/USDT" not in symbols


async def test_search_symbols_matches_quote(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        matches = await adapter.search_symbols("USDT")
        # Every active spot/swap quotes USDT here.
        assert len(matches) >= 4


async def test_search_symbols_respects_limit(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        matches = await adapter.search_symbols("USDT", limit=1)
        assert len(matches) == 1


async def test_search_symbols_market_type_filter(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        matches = await adapter.search_symbols("USDT", market_type="swap")
        assert {m.type for m in matches} == {"swap"}


# --------------------------------------------------------------------------- #
# market-data normalization + ms->UTC conversion
# --------------------------------------------------------------------------- #
async def test_fetch_ticker_normalizes(patch_client: Callable[..., FakeCcxt]) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        ticker = await adapter.fetch_ticker("BTC/USDT")
        assert isinstance(ticker, Ticker)
        assert ticker.exchange == "bybit"
        assert ticker.symbol == "BTC/USDT"
        assert ticker.last == 42000.5
        assert ticker.bid == 41999.0
        assert ticker.ask == 42001.0
        assert ticker.timestamp == FIXED_DT
        assert ticker.timestamp is not None
        assert ticker.timestamp.tzinfo is UTC


async def test_fetch_ohlcv_normalizes_and_skips_short_rows(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        result = await adapter.fetch_ohlcv("BTC/USDT", "1h", limit=10)
        assert isinstance(result, OHLCVResult)
        assert result.timeframe == "1h"
        # 3 valid rows; the 4th (short) row is skipped.
        assert result.count == 3
        assert len(result.bars) == 3
        first = result.bars[0]
        assert first.timestamp == FIXED_DT
        assert first.timestamp.tzinfo is UTC
        assert first.open == 41500.0
        assert first.close == 42000.0
        assert first.volume == 100.0


async def test_fetch_ohlcv_passes_since_as_ms(
    patch_client: Callable[..., FakeCcxt],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = patch_client()
    captured: dict[str, object] = {}
    original = client.fetch_ohlcv

    async def spy(symbol: str, timeframe: str, since: int | None, limit: int) -> object:
        captured["since"] = since
        captured["limit"] = limit
        return await original(symbol, timeframe, since, limit)

    monkeypatch.setattr(client, "fetch_ohlcv", spy)
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        since = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        await adapter.fetch_ohlcv("BTC/USDT", "1h", since=since, limit=7)
        assert captured["since"] == int(since.timestamp() * 1000)
        assert captured["limit"] == 7


async def test_fetch_order_book_normalizes_and_skips_short_levels(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        book = await adapter.fetch_order_book("BTC/USDT", limit=5)
        assert isinstance(book, OrderBook)
        assert book.symbol == "BTC/USDT"
        assert book.timestamp == FIXED_DT
        # The malformed (short) bid is skipped: 2 bids, 2 asks.
        assert len(book.bids) == 2
        assert len(book.asks) == 2
        assert book.bids[0].price == 41999.0
        assert book.bids[0].amount == 1.5


async def test_fetch_recent_trades_normalizes_and_skips_bad(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        result = await adapter.fetch_recent_trades("BTC/USDT", limit=50)
        assert isinstance(result, RecentTradesResult)
        # 2 valid trades; the priceless one is skipped.
        assert result.count == 2
        assert len(result.trades) == 2
        first = result.trades[0]
        assert first.id == "t1"
        assert first.side == "buy"
        assert first.price == 42000.0
        assert first.timestamp == FIXED_DT
        # Non-str id is coerced to str.
        assert result.trades[1].id == "2"


async def test_fetch_funding_rate_normalizes(
    patch_client: Callable[..., FakeCcxt],
) -> None:
    adapter = await _adapter(patch_client)
    async with adapter:
        funding = await adapter.fetch_funding_rate("ETH/USDT:USDT")
        assert isinstance(funding, FundingRate)
        assert funding.symbol == "ETH/USDT:USDT"
        assert funding.funding_rate == 0.0001
        assert funding.mark_price == 2500.5
        assert funding.index_price == 2500.0
        assert funding.interval == "8h"
        assert funding.timestamp == FIXED_DT
        expected_next = datetime.fromtimestamp((FIXED_MS + 28_800_000) / 1000, tz=UTC)
        assert funding.next_funding_time == expected_next
