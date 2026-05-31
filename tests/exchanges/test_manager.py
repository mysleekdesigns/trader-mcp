"""Tests for :class:`ExchangeManager` (adapter cache + lifecycle).

Covers per-(exchange, testnet) caching, rejection of unsupported ids, and that
``aclose_all`` closes every cached adapter's fake client and is idempotent.
"""

from __future__ import annotations

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FakeCcxt, make_create_factory
from trader_mcp.errors import ValidationError
from trader_mcp.exchanges import ExchangeAdapter, ExchangeManager

pytestmark = pytest.mark.usefixtures("no_credentials")


async def test_get_caches_one_adapter_per_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated get() for the same (exchange, testnet) returns the same instance."""
    clients = [FakeCcxt() for _ in range(4)]
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(clients=clients))
    manager = ExchangeManager()
    try:
        a1 = await manager.get("bybit")
        a2 = await manager.get("bybit")
        assert a1 is a2  # cached
        assert isinstance(a1, ExchangeAdapter)
    finally:
        await manager.aclose_all()


async def test_get_distinct_adapters_for_testnet_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = [FakeCcxt() for _ in range(4)]
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(clients=clients))
    manager = ExchangeManager()
    try:
        live = await manager.get("bybit", testnet=False)
        testnet = await manager.get("bybit", testnet=True)
        assert live is not testnet
        assert live.testnet is False
        assert testnet.testnet is True
    finally:
        await manager.aclose_all()


async def test_get_distinct_adapters_per_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = [FakeCcxt() for _ in range(4)]
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(clients=clients))
    manager = ExchangeManager()
    try:
        bybit = await manager.get("bybit")
        blofin = await manager.get("blofin")
        assert bybit is not blofin
        assert bybit.exchange_id == "bybit"
        assert blofin.exchange_id == "blofin"
    finally:
        await manager.aclose_all()


@pytest.mark.parametrize("bad", ["binance", "", "BYBIT", "okx"])
async def test_get_unsupported_raises_validation_error(bad: str) -> None:
    manager = ExchangeManager()
    try:
        with pytest.raises(ValidationError) as excinfo:
            await manager.get(bad)  # type: ignore[arg-type]
        assert excinfo.value.details["kind"] == "unsupported_exchange"
    finally:
        await manager.aclose_all()


async def test_aclose_all_closes_clients_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = [FakeCcxt() for _ in range(4)]
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(clients=clients))
    manager = ExchangeManager()
    a_bybit = await manager.get("bybit")
    a_blofin = await manager.get("blofin")

    await manager.aclose_all()
    # Both underlying fake clients were closed.
    assert a_bybit._client.closed is True  # type: ignore[attr-defined]
    assert a_blofin._client.closed is True  # type: ignore[attr-defined]

    # Safe to call twice (no error, no re-close storm).
    await manager.aclose_all()

    # After close, get() rebuilds a fresh adapter (cache was cleared).
    fresh = await manager.get("bybit")
    assert fresh is not a_bybit
    await manager.aclose_all()
