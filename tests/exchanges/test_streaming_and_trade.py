"""Offline tests for the Phase 5 connectivity slice of the exchange adapter.

Covers, with NO network (a fake CCXT Pro client injected behind the single
construction seam):

    * WebSocket streaming (``watch_ohlcv``/``watch_trades``/``watch_order_book``):
      typed normalization shape-identical to the REST path, bounded reconnection
      backoff on a transient fault, clean cancellation, and a typed refusal when
      WS is unsupported.
    * Sandbox/testnet wiring: ``set_sandbox_mode(True)`` is called when requested
      and supported; refused (typed error) when the exchange has no sandbox.
    * Trade plumbing (create/cancel/fetch order, open orders, positions, balance):
      typed normalization, ``clientOrderId`` forwarding, and -- the safety
      invariant -- refusal with ``details["kind"] == "scope"`` on a read-only key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import ccxt
import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FIXED_MS, FakeCcxt, ccxt_error, make_create_factory
from trader_mcp.config import ExchangeCredentials, KeyScope, Settings, get_settings
from trader_mcp.errors import ExchangeError
from trader_mcp.exchanges import ExchangeAdapter
from trader_mcp.exchanges.models import Balance, OHLCVBar, Order, OrderBook, Position, Trade


# --------------------------------------------------------------------------- #
# A CCXT Pro / trade-capable fake extending the shared market-data fake.
# --------------------------------------------------------------------------- #
class FakeProCcxt(FakeCcxt):
    """A :class:`FakeCcxt` extended with ``watch_*`` streams + order/account calls.

    The ``watch_*`` queues hold either a payload (yielded) or a ``BaseException``
    (raised) so reconnection-after-transient-fault is exercisable. When a queue is
    exhausted, the watcher blocks forever (cancellation-friendly) so tests drive it
    with ``asyncio.wait_for`` / explicit cancellation.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ohlcv_updates: list[Any] = []
        self.trades_updates: list[Any] = []
        self.order_book_updates: list[Any] = []
        self.order_data: dict[str, Any] = _order_payload()
        self.open_orders_data: list[dict[str, Any]] = [_order_payload(order_id="o2")]
        self.positions_data: list[dict[str, Any]] = [_position_payload()]
        self.created_params: dict[str, Any] | None = None
        self.supports_positions: bool = True

    # -- watch_* (CCXT Pro) ------------------------------------------------- #
    async def _next(self, method: str, queue: list[Any]) -> Any:
        self._dispatch(method)
        if not queue:
            # No more scripted updates: block until cancelled (a live stream waits).
            await asyncio.Event().wait()
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> Any:
        return await self._next("watch_ohlcv", self.ohlcv_updates)

    async def watch_trades(self, symbol: str) -> Any:
        return await self._next("watch_trades", self.trades_updates)

    async def watch_order_book(self, symbol: str, limit: int) -> Any:
        return await self._next("watch_order_book", self.order_book_updates)

    # -- orders / account --------------------------------------------------- #
    async def create_order(
        self,
        symbol: str,
        type: str,  # noqa: A002 -- mirrors CCXT's create_order signature
        side: str,
        amount: float,
        price: float | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self._dispatch("create_order")
        self.created_params = params
        return {**self.order_data, "symbol": symbol, "type": type, "side": side}

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self._dispatch("cancel_order")
        return {**self.order_data, "id": order_id, "symbol": symbol, "status": "canceled"}

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self._dispatch("fetch_order")
        return {**self.order_data, "id": order_id, "symbol": symbol}

    async def fetch_open_orders(self, symbol: str | None) -> list[dict[str, Any]]:
        self._dispatch("fetch_open_orders")
        return self.open_orders_data

    async def fetch_positions(self, symbols: list[str] | None) -> list[dict[str, Any]]:
        self._dispatch("fetch_positions")
        return self.positions_data

    def __getattribute__(self, name: str) -> Any:
        # Allow tests to hide fetch_positions support (Coinbase is spot-only) by
        # flipping ``supports_positions`` -- the adapter probes for the attribute.
        if name == "fetch_positions" and not object.__getattribute__(self, "supports_positions"):
            raise AttributeError(name)
        return object.__getattribute__(self, name)


def _order_payload(order_id: str = "o1") -> dict[str, Any]:
    return {
        "id": order_id,
        "clientOrderId": "cid-123",
        "symbol": "BTC/USD",
        "type": "limit",
        "side": "buy",
        "status": "open",
        "price": 42000.0,
        "amount": 0.5,
        "filled": 0.1,
        "remaining": 0.4,
        "average": 42000.0,
        "cost": 4200.0,
        "fee": {"cost": 2.1, "currency": "USD", "rate": 0.001},
        "timestamp": FIXED_MS,
    }


def _position_payload() -> dict[str, Any]:
    return {
        "symbol": "ETH/USD:USD",
        "side": "long",
        "contracts": 3.0,
        "contractSize": 1.0,
        "entryPrice": 2500.0,
        "markPrice": 2550.0,
        "notional": 7650.0,
        "unrealizedPnl": 150.0,
        "leverage": 5.0,
        "liquidationPrice": 2000.0,
        "timestamp": FIXED_MS,
    }


def _ohlcv_update() -> list[list[float]]:
    return [
        [FIXED_MS, 41500.0, 42100.0, 41400.0, 42000.0, 100.0],
        [FIXED_MS + 60_000, 42000.0, 42050.0],  # short row -> dropped
    ]


def _trades_update() -> list[dict[str, Any]]:
    return [
        {"id": "x1", "timestamp": FIXED_MS, "side": "buy", "price": 42000.0, "amount": 0.5},
        {"id": "x2", "timestamp": FIXED_MS, "side": "sell", "amount": 1.0},  # no price -> dropped
    ]


def _order_book_update() -> dict[str, Any]:
    return {
        "symbol": "BTC/USD",
        "timestamp": FIXED_MS,
        "bids": [[41999.0, 1.5], [41998.0, 2.0]],
        "asks": [[42001.0, 1.0]],
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_pro_client(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeProCcxt]:
    """Install a :class:`FakeProCcxt` behind the single construction seam."""

    def install(**kwargs: object) -> FakeProCcxt:
        client = FakeProCcxt(**kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
        return client

    return install


@pytest.fixture
def no_sleep_ws(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the adapter's ``_sleep`` to a no-op, recording requested delays."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(adapter_module, "_sleep", fake_sleep)
    return delays


@pytest.fixture
def trade_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every exchange to a configured TRADE_ENABLED credential."""
    from pydantic import SecretStr

    def creds(self: Settings, exchange: str) -> ExchangeCredentials:
        return ExchangeCredentials(
            api_key=SecretStr("k"),
            api_secret=SecretStr("s"),
            scope=KeyScope.TRADE_ENABLED,
        )

    monkeypatch.setattr(Settings, "credentials_for", creds)
    get_settings.cache_clear()


@pytest.fixture
def readonly_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every exchange to a configured READ_ONLY credential."""
    from pydantic import SecretStr

    def creds(self: Settings, exchange: str) -> ExchangeCredentials:
        return ExchangeCredentials(
            api_key=SecretStr("k"),
            api_secret=SecretStr("s"),
            scope=KeyScope.READ_ONLY,
        )

    monkeypatch.setattr(Settings, "credentials_for", creds)
    get_settings.cache_clear()


async def _collect(gen: Any, n: int) -> list[Any]:
    """Collect ``n`` items from an async generator, then aclose it."""
    out: list[Any] = []
    async for item in gen:
        out.append(item)
        if len(out) >= n:
            break
    await gen.aclose()
    return out


# --------------------------------------------------------------------------- #
# WebSocket streaming
# --------------------------------------------------------------------------- #
pytestmark = pytest.mark.usefixtures("no_credentials")


async def test_watch_ohlcv_yields_typed_bars(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    client = patch_pro_client()
    client.ohlcv_updates = [_ohlcv_update()]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        bars = await _collect(adapter.watch_ohlcv("BTC/USD", "1m"), 1)
    assert len(bars) == 1
    bar = bars[0]
    assert isinstance(bar, OHLCVBar)
    # Shape-identical to the REST path: the short row was dropped, body coerced.
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        41500.0,
        42100.0,
        41400.0,
        42000.0,
        100.0,
    )
    assert bar.timestamp.tzinfo is not None


async def test_watch_trades_yields_typed_trades(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    client = patch_pro_client()
    client.trades_updates = [_trades_update()]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        trades = await _collect(adapter.watch_trades("BTC/USD"), 1)
    assert len(trades) == 1  # the priceless trade was dropped
    assert isinstance(trades[0], Trade)
    assert trades[0].side == "buy"
    assert trades[0].price == 42000.0


async def test_watch_order_book_yields_typed_book(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    client = patch_pro_client()
    client.order_book_updates = [_order_book_update()]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        books = await _collect(adapter.watch_order_book("BTC/USD"), 1)
    assert len(books) == 1
    book = books[0]
    assert isinstance(book, OrderBook)
    assert [lvl.price for lvl in book.bids] == [41999.0, 41998.0]
    assert [lvl.price for lvl in book.asks] == [42001.0]


async def test_watch_reconnects_on_transient_then_succeeds(
    patch_pro_client: Callable[..., FakeProCcxt],
    no_sleep_ws: list[float],
) -> None:
    """A transient WS fault triggers bounded backoff, then the stream recovers."""
    client = patch_pro_client()
    client.ohlcv_updates = [
        ccxt_error("network", leak=False),  # transient: reconnect after backoff
        _ohlcv_update(),  # then a real update
    ]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        bars = await _collect(adapter.watch_ohlcv("BTC/USD", "1m"), 1)
    assert len(bars) == 1
    # Backed off exactly once (one transient failure) at the base WS delay.
    assert no_sleep_ws == [adapter_module.WS_BACKOFF_BASE_SECONDS]
    # watch_ohlcv was awaited twice (failure + success).
    assert client.calls["watch_ohlcv"] == 2


async def test_watch_reraises_non_transient_mapped(
    patch_pro_client: Callable[..., FakeProCcxt],
    no_sleep_ws: list[float],
) -> None:
    """An auth fault on a WS stream is mapped (not reconnected)."""
    client = patch_pro_client()
    client.trades_updates = [ccxt_error("auth", leak=False)]
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await _collect(adapter.watch_trades("BTC/USD"), 1)
    assert excinfo.value.details["kind"] == "auth"
    assert no_sleep_ws == []  # no reconnection backoff for a non-transient fault


async def test_watch_cancellation_is_clean(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    """Cancelling a blocked watcher task propagates CancelledError, not a wrapped one."""
    patch_pro_client()  # empty queues -> watcher blocks forever
    adapter = await ExchangeAdapter.create("coinbase")

    async def consume() -> None:
        async for _bar in adapter.watch_ohlcv("BTC/USD", "1m"):
            pass

    async with adapter:
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let it reach the blocking await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_watch_unsupported_raises_typed(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    """A client without a watch_* method raises a typed not_supported error."""
    client = patch_pro_client()
    # Remove the WS method to simulate a build without CCXT Pro support.
    object.__setattr__(client, "watch_order_book", None)
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await _collect(adapter.watch_order_book("BTC/USD"), 1)
    assert excinfo.value.details["kind"] == "not_supported"


# --------------------------------------------------------------------------- #
# Sandbox / testnet wiring
# --------------------------------------------------------------------------- #
async def test_sandbox_enabled_when_requested(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    client = patch_pro_client()  # default fake has urls.test set
    adapter = await ExchangeAdapter.create("coinbase", testnet=True)
    async with adapter:
        assert adapter.testnet is True
        assert client.sandbox_calls == [True]


async def test_sandbox_alias_overrides_testnet(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    client = patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase", testnet=False, sandbox=True)
    async with adapter:
        assert client.sandbox_calls == [True]


async def test_sandbox_refused_when_unsupported(
    patch_pro_client: Callable[..., FakeProCcxt],
) -> None:
    """No urls.test -> sandbox is refused with a typed error (never silent live)."""
    client = patch_pro_client(urls={})  # no "test" endpoint
    with pytest.raises(ExchangeError) as excinfo:
        await ExchangeAdapter.create("coinbase", testnet=True)
    assert excinfo.value.details["kind"] == "not_supported"
    assert client.sandbox_calls == []


# --------------------------------------------------------------------------- #
# Trade plumbing: scope refusal (read-only) + normalization (trade-enabled)
# --------------------------------------------------------------------------- #
async def test_create_order_refused_read_only(
    patch_pro_client: Callable[..., FakeProCcxt],
    readonly_credentials: None,
) -> None:
    client = patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    assert adapter.key_scope is KeyScope.READ_ONLY
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await adapter.create_order("BTC/USD", "limit", "buy", 0.5, 42000.0)
        assert excinfo.value.details["kind"] == "scope"
        # Structurally unable: the CCXT call was never reached.
        assert "create_order" not in client.calls


@pytest.mark.parametrize(
    "invoke",
    [
        lambda a: a.create_order("BTC/USD", "limit", "buy", 0.5, 42000.0),
        lambda a: a.cancel_order("o1", "BTC/USD"),
        lambda a: a.fetch_order("o1", "BTC/USD"),
        lambda a: a.fetch_open_orders(),
        lambda a: a.fetch_positions(),
        lambda a: a.fetch_balance(),
    ],
)
async def test_all_trade_methods_refuse_read_only(
    patch_pro_client: Callable[..., FakeProCcxt],
    readonly_credentials: None,
    invoke: Callable[[ExchangeAdapter], Any],
) -> None:
    patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        with pytest.raises(ExchangeError) as excinfo:
            await invoke(adapter)
        assert excinfo.value.details["kind"] == "scope"


async def test_create_order_forwards_client_order_id(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    client = patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    assert adapter.key_scope is KeyScope.TRADE_ENABLED
    async with adapter:
        order = await adapter.create_order(
            "BTC/USD", "limit", "buy", 0.5, 42000.0, client_order_id="my-cid"
        )
    assert isinstance(order, Order)
    assert client.created_params is not None
    assert client.created_params["clientOrderId"] == "my-cid"


async def test_create_order_merges_extra_params(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    client = patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        await adapter.create_order(
            "BTC/USD",
            "limit",
            "buy",
            0.5,
            42000.0,
            client_order_id="cid",
            params={"postOnly": True},
        )
    assert client.created_params == {"postOnly": True, "clientOrderId": "cid"}


async def test_order_normalization(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        order = await adapter.fetch_order("o1", "BTC/USD")
    assert order.id == "o1"
    assert order.client_order_id == "cid-123"
    assert order.type == "limit"
    assert order.side == "buy"
    assert order.status == "open"
    assert order.filled == 0.1
    assert order.remaining == 0.4
    assert order.fee is not None
    assert order.fee.cost == 2.1
    assert order.fee.currency == "USD"
    assert order.timestamp is not None


async def test_cancel_order_sets_canceled_status(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        order = await adapter.cancel_order("o9", "BTC/USD")
    assert order.id == "o9"
    assert order.status == "canceled"


async def test_fetch_open_orders_normalizes_list(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    patch_pro_client()
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        orders = await adapter.fetch_open_orders("BTC/USD")
    assert len(orders) == 1
    assert isinstance(orders[0], Order)
    assert orders[0].id == "o2"


async def test_fetch_positions_normalizes(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    patch_pro_client()
    adapter = await ExchangeAdapter.create("kraken")
    async with adapter:
        positions = await adapter.fetch_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert isinstance(pos, Position)
    assert pos.symbol == "ETH/USD:USD"
    assert pos.side == "long"
    assert pos.contracts == 3.0
    assert pos.unrealized_pnl == 150.0
    assert pos.leverage == 5.0
    assert pos.liquidation_price == 2000.0


async def test_fetch_positions_empty_when_unsupported(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    """A spot-only exchange (no fetch_positions) returns [] rather than raising."""
    client = patch_pro_client()
    client.supports_positions = False
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        positions = await adapter.fetch_positions()
    assert positions == []


async def test_fetch_balance_normalizes(
    patch_pro_client: Callable[..., FakeProCcxt],
    trade_credentials: None,
) -> None:
    client = patch_pro_client()
    client.balance_data = {
        "USD": {"free": 100.0, "used": 25.0, "total": 125.0},
        "BTC": {"free": 0.0, "used": 0.0, "total": 0.0},  # zero -> dropped
        "free": {"USD": 100.0},  # aggregate key -> dropped
        "timestamp": FIXED_MS,
        "info": {"raw": "x"},
    }
    adapter = await ExchangeAdapter.create("coinbase")
    async with adapter:
        balance = await adapter.fetch_balance()
    assert isinstance(balance, Balance)
    assert set(balance.entries) == {"USD"}
    assert balance.entries["USD"].total == 125.0
    assert balance.entries["USD"].used == 25.0
    assert balance.timestamp is not None


def test_unsupported_market_status_degrades() -> None:
    """A raw status outside the known set normalizes to 'unknown'."""
    assert ExchangeAdapter._normalize_status("partially_filled") == "unknown"
    assert ExchangeAdapter._normalize_status(None) == "unknown"
    assert ExchangeAdapter._normalize_status("closed") == "closed"


def test_sandbox_helper_passthrough() -> None:
    """A non-ccxt client object with no urls.test is refused, not silently live."""

    class _NoSandbox:
        def __init__(self) -> None:
            self.urls: dict[str, Any] = {}

        def set_sandbox_mode(self, enabled: bool) -> None:  # pragma: no cover
            raise AssertionError("must not be called")

    with pytest.raises(ExchangeError) as excinfo:
        ExchangeAdapter._enable_sandbox(_NoSandbox(), "coinbase")
    assert excinfo.value.details["kind"] == "not_supported"


def test_ccxt_notsupported_is_mapped() -> None:
    """A CCXT NotSupported raised by set_sandbox_mode is mapped, not raw."""

    class _Raises:
        def __init__(self) -> None:
            self.urls: dict[str, Any] = {"test": "https://t"}

        def set_sandbox_mode(self, enabled: bool) -> None:
            raise ccxt.NotSupported("nope")

    with pytest.raises(ExchangeError) as excinfo:
        ExchangeAdapter._enable_sandbox(_Raises(), "coinbase")
    assert excinfo.value.details["kind"] == "not_supported"
