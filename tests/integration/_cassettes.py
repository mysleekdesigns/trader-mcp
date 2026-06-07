"""A tiny, dependency-free VCR-style cassette mechanism for offline integration tests.

Rather than pull in a heavy HTTP-record/replay dependency (vcrpy/betamax), we record
**sanitized JSON fixtures of the CCXT responses** the adapter actually consumes and
replay them through the real :class:`~trader_mcp.exchanges.ExchangeAdapter`. This keeps
the integration tests deterministic and fully offline while still exercising the genuine
adapter -> tool normalization path against realistic exchange payloads.

The replay client (:class:`CassetteCcxt`) reuses the exact attribute/method surface the
adapter touches (it is a focused sibling of :class:`tests._fakes.FakeCcxt`), so the same
monkeypatch seam -- ``trader_mcp.exchanges.adapter._create_ccxt_client`` -- wires it in.

Fixtures live under ``tests/fixtures/cassettes/<exchange>/``. They are SANITIZED: public
market data only, no API keys, no account identifiers, no PII. The ``_comment`` key in
each file documents provenance and the sanitization done; the loader strips ``_comment``
before the payload reaches the adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Root of the recorded-cassette fixtures.
CASSETTE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "cassettes"


def _strip_comments(obj: Any) -> Any:
    """Recursively drop ``_comment`` annotation keys from loaded JSON."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if k != "_comment"}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load_cassette(exchange: str, name: str) -> Any:
    """Load + de-annotate a sanitized cassette ``fixtures/cassettes/<exchange>/<name>.json``.

    Asserts the payload carries no secret-shaped material so a fixture can never
    smuggle a key/PII into the suite.
    """
    path = CASSETTE_ROOT / exchange / f"{name}.json"
    raw = path.read_text(encoding="utf-8")
    assert_sanitized(raw, where=str(path))
    return _strip_comments(json.loads(raw))


#: Substrings that must never appear in a sanitized cassette (defense-in-depth).
_FORBIDDEN_SUBSTRINGS = (
    "apiKey",
    "api_key",
    "secret",
    "password",
    "passphrase",
    "BEGIN PRIVATE KEY",
    "Authorization",
)


def assert_sanitized(raw: str, *, where: str) -> None:
    """Fail loudly if a cassette appears to contain secret/credential material."""
    lowered = raw.lower()
    for token in _FORBIDDEN_SUBSTRINGS:
        assert token.lower() not in lowered, f"cassette {where} contains forbidden token {token!r}"


class CassetteCcxt:
    """A replay-only async CCXT stand-in backed by sanitized JSON cassettes.

    Mirrors the subset of the CCXT async client surface the adapter uses, returning
    canned payloads loaded from ``tests/fixtures/cassettes/<exchange>/``. Every call
    is counted so tests can assert the adapter actually invoked the recorded path.
    """

    def __init__(self, exchange: str) -> None:
        self._exchange = exchange
        markets = load_cassette(exchange, "markets")
        # Coinbase (reference) is spot-only -> no funding-rate surface; the others we
        # record likewise have no perps here. ``fetchFundingRate`` is therefore absent.
        self.has: dict[str, Any] = {
            "fetchOHLCV": True,
            "fetchOrderBook": True,
            "fetchTrades": True,
            "fetchTicker": True,
            "spot": True,
            "swap": False,
        }
        self.timeframes: dict[str, Any] = {"1m": "1m", "5m": "5m", "1h": "1h", "1d": "1d"}
        self._initial_markets: dict[str, Any] = markets
        self.markets: dict[str, Any] | None = None
        self.urls: dict[str, Any] = {}

        self.calls: dict[str, int] = {}
        self.sandbox_calls: list[bool] = []
        self.closed: bool = False

    # -- helpers ---------------------------------------------------------- #
    def _count(self, method: str) -> None:
        self.calls[method] = self.calls.get(method, 0) + 1

    @staticmethod
    def _symbol_slug(symbol: str) -> str:
        return symbol.replace("/", "_").replace(":", "_")

    # -- markets ---------------------------------------------------------- #
    async def load_markets(self, reload: bool = False) -> dict[str, Any]:
        self._count("load_markets")
        self.markets = dict(self._initial_markets)
        return self.markets

    # -- public market data ---------------------------------------------- #
    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self._count("fetch_ticker")
        return load_cassette(self._exchange, f"ticker_{self._symbol_slug(symbol)}")

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None, limit: int
    ) -> list[list[float]]:
        self._count("fetch_ohlcv")
        data = load_cassette(self._exchange, f"ohlcv_{self._symbol_slug(symbol)}_{timeframe}")
        return data["bars"]

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        self._count("fetch_order_book")
        return load_cassette(self._exchange, f"order_book_{self._symbol_slug(symbol)}")

    async def fetch_trades(
        self, symbol: str, since: int | None, limit: int
    ) -> list[dict[str, Any]]:
        self._count("fetch_trades")
        data = load_cassette(self._exchange, f"trades_{self._symbol_slug(symbol)}")
        return data["trades"]

    # -- lifecycle / config ---------------------------------------------- #
    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox_calls.append(enabled)

    async def close(self) -> None:
        self.closed = True
