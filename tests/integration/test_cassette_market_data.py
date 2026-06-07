"""VCR-style recorded-fixture integration tests: cassette -> adapter normalization.

Deterministic and fully offline. Each test replays a SANITIZED recorded CCXT payload
(``tests/fixtures/cassettes/<exchange>/``) through the real
:class:`~trader_mcp.exchanges.ExchangeAdapter` and asserts on the typed Pydantic v2
output -- proving the genuine adapter normalization path against realistic exchange
data, not just hand-built fakes. Coinbase (the reference exchange) is exercised in full;
Kraken covers a second venue's notation.

These are the offline analogue of the opt-in live suite (``tests/live/``): same
invariants, same assertions, but driven from recorded cassettes so they run in the
default gate with no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests.integration._cassettes import CassetteCcxt, load_cassette
from trader_mcp.config import ExchangeId
from trader_mcp.exchanges import (
    ExchangeAdapter,
    OHLCVResult,
    OrderBook,
    RecentTradesResult,
    Ticker,
)

# Cassette tests run fully offline and with no real credentials.
pytestmark = pytest.mark.usefixtures("no_credentials")


@pytest.fixture
def install_cassettes(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], CassetteCcxt]:
    """Wire a :class:`CassetteCcxt` for ``exchange`` behind the client-construction seam."""

    def install(exchange: str) -> CassetteCcxt:
        client = CassetteCcxt(exchange)

        def factory(exchange_id: str, config: dict[str, object]) -> CassetteCcxt:
            return client

        monkeypatch.setattr(adapter_module, "_create_ccxt_client", factory)
        return client

    return install


@pytest.fixture
async def cassette_adapter(
    install_cassettes: Callable[[str], CassetteCcxt],
) -> AsyncIterator[Callable[[ExchangeId], Awaitable[ExchangeAdapter]]]:
    """Yield a builder that returns a closed-on-teardown adapter for an exchange."""
    adapters: list[ExchangeAdapter] = []

    async def build(exchange: ExchangeId) -> ExchangeAdapter:
        install_cassettes(exchange)
        adapter = await ExchangeAdapter.create(exchange)
        adapters.append(adapter)
        return adapter

    try:
        yield build
    finally:
        for adapter in adapters:
            await adapter.aclose()


# --------------------------------------------------------------------------- #
# Coinbase (reference exchange) -- full path coverage
# --------------------------------------------------------------------------- #
async def test_coinbase_capabilities_from_cassette(cassette_adapter: Callable) -> None:
    adapter = await cassette_adapter("coinbase")
    await adapter.load_markets()
    caps = adapter.capabilities()
    assert caps.exchange == "coinbase"
    assert caps.market_count is not None
    assert caps.market_count > 0
    assert caps.supports_ohlcv
    assert caps.supports_ticker
    # Recorded Coinbase cassette is spot-only -> no funding surface.
    assert caps.supports_funding_rate is False


async def test_coinbase_list_and_search_markets_from_cassette(
    cassette_adapter: Callable,
) -> None:
    adapter = await cassette_adapter("coinbase")
    markets = await adapter.list_markets(limit=10)
    assert markets
    # ``active_only`` default drops the inactive ETH/USDC pair in the cassette.
    assert all(m.active for m in markets)
    matches = await adapter.search_symbols("BTC", limit=10)
    assert matches
    assert all("btc" in m.symbol.lower() or m.base.lower() == "btc" for m in matches)


async def test_coinbase_ticker_from_cassette(cassette_adapter: Callable) -> None:
    adapter = await cassette_adapter("coinbase")
    ticker = await adapter.fetch_ticker("BTC/USD")
    assert isinstance(ticker, Ticker)
    assert ticker.bid is not None
    assert ticker.ask is not None
    assert ticker.bid < ticker.ask
    assert ticker.last == pytest.approx(62500.75)


async def test_coinbase_ohlcv_invariants_from_cassette(cassette_adapter: Callable) -> None:
    adapter = await cassette_adapter("coinbase")
    result = await adapter.fetch_ohlcv("BTC/USD", "1h", limit=100)
    assert isinstance(result, OHLCVResult)
    assert result.bars
    timestamps = [b.timestamp for b in result.bars]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert all(b.timestamp.tzinfo is not None for b in result.bars)
    for bar in result.bars:
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)


async def test_coinbase_order_book_from_cassette(cassette_adapter: Callable) -> None:
    adapter = await cassette_adapter("coinbase")
    book = await adapter.fetch_order_book("BTC/USD", limit=10)
    assert isinstance(book, OrderBook)
    assert book.bids
    assert book.asks
    assert book.bids[0].price < book.asks[0].price
    # bids descending, asks ascending.
    bid_prices = [lvl.price for lvl in book.bids]
    ask_prices = [lvl.price for lvl in book.asks]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)


async def test_coinbase_recent_trades_from_cassette(cassette_adapter: Callable) -> None:
    adapter = await cassette_adapter("coinbase")
    result = await adapter.fetch_recent_trades("BTC/USD", limit=20)
    assert isinstance(result, RecentTradesResult)
    assert result.trades
    assert all(t.price > 0 and t.amount > 0 for t in result.trades)


# --------------------------------------------------------------------------- #
# Kraken (non-reference) -- a second venue's notation
# --------------------------------------------------------------------------- #
async def test_kraken_smoke_from_cassette(cassette_adapter: Callable) -> None:
    adapter = await cassette_adapter("kraken")
    markets = await adapter.list_markets(active_only=True)
    assert markets
    ticker = await adapter.fetch_ticker("BTC/USD")
    assert ticker.bid is not None
    assert ticker.ask is not None
    assert ticker.bid < ticker.ask
    ohlcv = await adapter.fetch_ohlcv("BTC/USD", "1h", limit=10)
    assert ohlcv.bars


# --------------------------------------------------------------------------- #
# The cassette client is actually exercised (replay, not a stub)
# --------------------------------------------------------------------------- #
async def test_cassette_records_calls(
    install_cassettes: Callable[[str], CassetteCcxt],
) -> None:
    client = install_cassettes("coinbase")
    adapter = await ExchangeAdapter.create("coinbase")
    try:
        await adapter.fetch_ticker("BTC/USD")
        await adapter.fetch_ohlcv("BTC/USD", "1h", since=None, limit=5)
    finally:
        await adapter.aclose()
    assert client.calls.get("fetch_ticker") == 1
    assert client.calls.get("fetch_ohlcv") == 1


# --------------------------------------------------------------------------- #
# Sanitization guarantee -- no cassette may carry secret/PII material
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("exchange", "name"),
    [
        ("coinbase", "markets"),
        ("coinbase", "ticker_BTC_USD"),
        ("coinbase", "ohlcv_BTC_USD_1h"),
        ("coinbase", "order_book_BTC_USD"),
        ("coinbase", "trades_BTC_USD"),
        ("kraken", "markets"),
        ("kraken", "ticker_BTC_USD"),
        ("kraken", "ohlcv_BTC_USD_1h"),
    ],
)
def test_cassettes_are_sanitized(exchange: str, name: str) -> None:
    # ``load_cassette`` runs ``assert_sanitized`` internally; a successful load proves
    # the fixture carries no apiKey/secret/passphrase/Authorization material.
    payload = load_cassette(exchange, name)
    assert payload is not None
