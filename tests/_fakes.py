"""Deterministic fake CCXT client + fixtures for fully-offline Phase 1 tests.

The exchange adapter touches a CCXT client only through a small, stable set of
attribute/method names (see ``ExchangeAdapter``). A lightweight fake that mimics
those shapes -- returning canned, CCXT-flavoured raw payloads -- lets the whole
exchange + market-data test suite run with **no network** and full determinism.

Wiring (the single offline seam): tests monkeypatch
``trader_mcp.exchanges.adapter._create_ccxt_client`` to return a :class:`FakeCcxt`,
so ``ExchangeAdapter.create(...)`` / ``ExchangeManager.get(...)`` build adapters
around the fake. ``_sleep`` is patched to a no-op so retry/backoff tests do not
actually sleep.

Every fake ``close()`` is an async no-op, so adapters/managers close cleanly and
no "unclosed client" resource warning trips ``filterwarnings = ["error"]``.

Secret-shaped constants live here too: a >=32-char opaque token the redaction
filter must scrub when it flows through the error path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import ccxt

# --------------------------------------------------------------------------- #
# Secret-shaped material (used to prove redaction on the error path)
# --------------------------------------------------------------------------- #
#: A >= 32-char opaque token shaped like an exchange API secret. The redaction
#: filter's long-token rule (>= 32 chars) must scrub this anywhere it appears.
SECRET_TOKEN = "SECRET_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

#: A ccxt error message embedding the secret as both a bare token and a keyed
#: ``apiKey=`` pair, to exercise both redaction rules.
SECRET_LEAK_MESSAGE = (
    f"coinbase GET /v2/accounts failed: apiKey={SECRET_TOKEN} signature mismatch {SECRET_TOKEN}"
)


def assert_no_secret(text: str) -> None:
    """Assert ``text`` does not contain the plaintext secret token."""
    assert SECRET_TOKEN not in text, f"secret leaked into: {text!r}"


# --------------------------------------------------------------------------- #
# CCXT market-dict builders (raw shapes the adapter normalizes)
# --------------------------------------------------------------------------- #
def spot_market(
    symbol: str = "BTC/USD",
    *,
    base: str = "BTC",
    quote: str = "USD",
    active: bool = True,
) -> dict[str, Any]:
    """A CCXT spot market dict."""
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "settle": None,
        "type": "spot",
        "spot": True,
        "swap": False,
        "active": active,
        "contractSize": None,
        "linear": None,
        "inverse": None,
        "precision": {"price": 0.01, "amount": 0.0001},
        "limits": {"amount": {"min": 0.0001, "max": 1000.0}, "cost": {"min": 5.0}},
        "maker": 0.001,
        "taker": 0.0015,
    }


def swap_market(
    symbol: str = "ETH/USD:USD",
    *,
    base: str = "ETH",
    quote: str = "USD",
    settle: str = "USD",
    active: bool = True,
) -> dict[str, Any]:
    """A CCXT linear perpetual-swap market dict."""
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "settle": settle,
        "type": "swap",
        "spot": False,
        "swap": True,
        "active": active,
        "contractSize": 1.0,
        "linear": True,
        "inverse": False,
        "precision": {"price": 0.05, "amount": 0.01},
        "limits": {"amount": {"min": 0.01, "max": 5000.0}, "cost": {"min": 1.0}},
        "maker": 0.0002,
        "taker": 0.00055,
    }


def option_market(symbol: str = "BTC/USD:BTC-251226-100000-C") -> dict[str, Any]:
    """A CCXT option market dict -- an UNSUPPORTED type the adapter must drop.

    No ``spot``/``swap`` flags and ``type`` is neither "spot" nor "swap", so
    normalization must return ``None`` for it.
    """
    return {
        "symbol": symbol,
        "base": "BTC",
        "quote": "USD",
        "settle": "BTC",
        "type": "option",
        "spot": False,
        "swap": False,
        "option": True,
        "active": True,
        "contractSize": 1.0,
        "linear": None,
        "inverse": True,
        "precision": {"price": 0.1, "amount": 0.001},
        "limits": {"amount": {"min": 0.001, "max": 100.0}, "cost": {"min": None}},
        "maker": 0.0003,
        "taker": 0.0003,
    }


def default_markets() -> dict[str, dict[str, Any]]:
    """A CCXT ``{symbol: market}`` map: spot + swap + one unsupported option.

    The option must be dropped by normalization, leaving exactly the spot and
    swap markets.
    """
    markets = [
        spot_market("BTC/USD", base="BTC", quote="USD"),
        spot_market("ETH/USD", base="ETH", quote="USD"),
        spot_market("SOL/BTC", base="SOL", quote="BTC", active=False),
        swap_market("ETH/USD:USD", base="ETH", quote="USD"),
        swap_market("BTC/USD:USD", base="BTC", quote="USD"),
        option_market(),
    ]
    return {m["symbol"]: m for m in markets}


# --------------------------------------------------------------------------- #
# Raw market-data payloads (CCXT shapes)
# --------------------------------------------------------------------------- #
#: A fixed UTC millisecond timestamp: 2024-01-02T03:04:05Z.
FIXED_MS = 1_704_164_645_000


def ticker_payload(symbol: str = "BTC/USD") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp": FIXED_MS,
        "last": 42000.5,
        "bid": 41999.0,
        "ask": 42001.0,
        "high": 43000.0,
        "low": 41000.0,
        "open": 41500.0,
        "close": 42000.5,
        "baseVolume": 1234.5,
        "quoteVolume": 51_000_000.0,
        "change": 500.5,
        "percentage": 1.2,
    }


def ohlcv_payload() -> list[list[float]]:
    """Three good rows + one short (malformed) row that must be skipped."""
    return [
        [FIXED_MS, 41500.0, 42100.0, 41400.0, 42000.0, 100.0],
        [FIXED_MS + 3_600_000, 42000.0, 42500.0, 41900.0, 42300.0, 120.0],
        [FIXED_MS + 7_200_000, 42300.0, 42800.0, 42200.0, 42700.0, 95.0],
        [FIXED_MS + 10_800_000, 42700.0],  # short row -> skipped
    ]


def order_book_payload(symbol: str = "BTC/USD") -> dict[str, Any]:
    """An order book with one malformed (short) level per side, to be skipped."""
    return {
        "symbol": symbol,
        "timestamp": FIXED_MS,
        "bids": [[41999.0, 1.5], [41998.0, 2.0], [41997.0]],  # last bid short -> skipped
        "asks": [[42001.0, 1.0], [42002.0, 3.0]],
    }


def trades_payload() -> list[dict[str, Any]]:
    """Recent trades with one malformed (priceless) trade that must be skipped."""
    return [
        {
            "id": "t1",
            "timestamp": FIXED_MS,
            "side": "buy",
            "price": 42000.0,
            "amount": 0.5,
            "cost": 21000.0,
        },
        {
            "id": 2,  # non-str id -> coerced to "2"
            "timestamp": FIXED_MS + 1000,
            "side": "sell",
            "price": 42001.0,
            "amount": 0.25,
            "cost": 10500.25,
        },
        {  # malformed: no price -> skipped
            "id": "t3",
            "timestamp": FIXED_MS + 2000,
            "side": "buy",
            "amount": 1.0,
        },
    ]


def funding_rate_payload(symbol: str = "ETH/USD:USD") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "fundingRate": 0.0001,
        "timestamp": FIXED_MS,
        "fundingTimestamp": FIXED_MS + 28_800_000,
        "markPrice": 2500.5,
        "indexPrice": 2500.0,
        "interval": "8h",
    }


# --------------------------------------------------------------------------- #
# The fake CCXT async client
# --------------------------------------------------------------------------- #
class FakeCcxt:
    """A minimal async stand-in for a ``ccxt.async_support`` client.

    Only the attributes/methods the adapter actually touches are implemented.
    Methods can be made to fail (or fail-then-succeed) via the ``fail_*`` knobs
    so error-mapping and retry/backoff are exercisable without any network.
    """

    def __init__(
        self,
        *,
        has: dict[str, Any] | None = None,
        timeframes: dict[str, Any] | None = None,
        markets: dict[str, dict[str, Any]] | None = None,
        urls: dict[str, Any] | None = None,
    ) -> None:
        self.has: dict[str, Any] = (
            has
            if has is not None
            else {
                "fetchOHLCV": True,
                "fetchOrderBook": True,
                "fetchTrades": True,
                "fetchTicker": True,
                "fetchFundingRate": "emulated",
                "spot": True,
                "swap": True,
            }
        )
        self.timeframes: dict[str, Any] = (
            timeframes if timeframes is not None else {"1m": "1", "1h": "60", "1d": "D"}
        )
        # CCXT exposes ``.markets`` as ``None`` until load_markets() runs.
        self._initial_markets = default_markets() if markets is None else markets
        self.markets: dict[str, Any] | None = None
        self.urls: dict[str, Any] = (
            urls if urls is not None else {"test": "https://testnet.example"}
        )

        # Behavior knobs.
        self.ticker_data = ticker_payload()
        self.ohlcv_data = ohlcv_payload()
        self.order_book_data = order_book_payload()
        self.trades_data = trades_payload()
        self.funding_data = funding_rate_payload()
        self.balance_data: dict[str, Any] = {"USD": {"free": 100.0, "used": 0.0, "total": 100.0}}

        #: Per-method exception to raise on EVERY call (persistent failure).
        self.raise_on: dict[str, BaseException] = {}
        #: Per-method (exc, n): raise ``exc`` the first ``n`` calls, then succeed.
        self.transient: dict[str, tuple[BaseException, int]] = {}

        #: Call counters keyed by method name.
        self.calls: dict[str, int] = {}
        self.sandbox_calls: list[bool] = []
        self.closed: bool = False

    # -- failure dispatch -------------------------------------------------- #
    def _dispatch(self, method: str) -> None:
        self.calls[method] = self.calls.get(method, 0) + 1
        if method in self.raise_on:
            raise self.raise_on[method]
        if method in self.transient:
            exc, remaining = self.transient[method]
            if remaining > 0:
                self.transient[method] = (exc, remaining - 1)
                raise exc

    # -- markets ----------------------------------------------------------- #
    async def load_markets(self, reload: bool = False) -> dict[str, Any]:
        self._dispatch("load_markets")
        self.markets = dict(self._initial_markets)
        return self.markets

    # -- public market data ------------------------------------------------ #
    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self._dispatch("fetch_ticker")
        return self.ticker_data

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None,
        limit: int,
    ) -> list[list[float]]:
        self._dispatch("fetch_ohlcv")
        return self.ohlcv_data

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        self._dispatch("fetch_order_book")
        return self.order_book_data

    async def fetch_trades(
        self, symbol: str, since: int | None, limit: int
    ) -> list[dict[str, Any]]:
        self._dispatch("fetch_trades")
        return self.trades_data

    async def fetch_funding_rate(self, symbol: str) -> dict[str, Any]:
        self._dispatch("fetch_funding_rate")
        return self.funding_data

    # -- private (read) ---------------------------------------------------- #
    async def fetch_balance(self) -> dict[str, Any]:
        self._dispatch("fetch_balance")
        return self.balance_data

    # -- lifecycle / config ------------------------------------------------ #
    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox_calls.append(enabled)

    async def close(self) -> None:
        self.closed = True


def make_create_factory(
    client: FakeCcxt | None = None,
    *,
    clients: list[FakeCcxt] | None = None,
) -> Callable[[str, dict[str, Any]], FakeCcxt]:
    """Return a ``_create_ccxt_client`` replacement that yields fake client(s).

    Pass a single ``client`` to always return it, or ``clients`` to return a
    fresh one per construction (for manager caching/multi-exchange tests).
    """
    pool: list[FakeCcxt] = list(clients) if clients is not None else []

    def factory(exchange_id: str, config: dict[str, Any]) -> FakeCcxt:
        if pool:
            return pool.pop(0)
        return client if client is not None else FakeCcxt()

    return factory


# --------------------------------------------------------------------------- #
# CCXT error factories (each embeds the secret token in its message)
# --------------------------------------------------------------------------- #
def ccxt_error(kind: str, *, leak: bool = True) -> ccxt.BaseError:
    """Build a representative CCXT exception of ``kind`` for error-mapping tests.

    When ``leak`` is True (default), the message embeds the secret token so the
    redaction guarantee on the error path can be asserted.
    """
    message = SECRET_LEAK_MESSAGE if leak else f"{kind} failure"
    factories: dict[str, Callable[[str], ccxt.BaseError]] = {
        "bad_symbol": ccxt.BadSymbol,
        "rate_limit": ccxt.RateLimitExceeded,
        "auth": ccxt.AuthenticationError,
        "network": ccxt.NetworkError,
        "not_supported": ccxt.NotSupported,
        "exchange": ccxt.BaseError,
    }
    return factories[kind](message)


#: (kind, exception, expected ``details["kind"]``) rows for parametrized mapping.
ERROR_KIND_CASES: Sequence[tuple[str, str]] = (
    ("bad_symbol", "bad_symbol"),
    ("rate_limit", "rate_limit"),
    ("auth", "auth"),
    ("network", "network"),
    ("not_supported", "not_supported"),
    ("exchange", "exchange"),
)
