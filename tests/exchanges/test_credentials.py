"""Tests for ``ExchangeAdapter.validate_credentials`` (read-only, no leaks).

Three paths:
    * No credentials configured -> immediate result, NO network call.
    * Configured + a successful private read -> valid, read-only scope, can't trade.
    * Configured + an auth failure -> invalid, redacted message (no secret).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import (
    SECRET_TOKEN,
    FakeCcxt,
    assert_no_secret,
    ccxt_error,
    make_create_factory,
)
from trader_mcp.config import ExchangeCredentials, Settings, get_settings
from trader_mcp.exchanges import CredentialStatus, ExchangeAdapter


async def test_no_credentials_returns_unknown_without_network(
    patch_client: Callable[..., FakeCcxt],
    no_credentials: None,
) -> None:
    """With no creds: configured=False, valid=None, scope=unknown, no balance call."""
    client = patch_client()
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        status = await adapter.validate_credentials()
        assert isinstance(status, CredentialStatus)
        assert status.configured is False
        assert status.valid is None
        assert status.scope == "unknown"
        assert status.can_trade is False
        # CRITICAL: no network -- fetch_balance was never called.
        assert client.calls.get("fetch_balance", 0) == 0


@pytest.fixture
def with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Settings.credentials_for`` return a configured read-only credential."""

    def configured(self: Settings, exchange: str) -> ExchangeCredentials:
        return ExchangeCredentials(
            api_key="public-key-value",  # type: ignore[arg-type]
            api_secret=SECRET_TOKEN,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(Settings, "credentials_for", configured)
    get_settings.cache_clear()


async def test_valid_credentials_report_read_only(
    monkeypatch: pytest.MonkeyPatch,
    with_credentials: None,
) -> None:
    """Configured creds + a successful private read -> valid, read-only, no trade."""
    client = FakeCcxt()
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        status = await adapter.validate_credentials()
        assert status.configured is True
        assert status.valid is True
        assert status.scope == "read_only"
        assert status.can_read is True
        assert status.can_trade is False
        # The private read WAS performed exactly once.
        assert client.calls.get("fetch_balance", 0) == 1


async def test_invalid_credentials_redacted_message(
    monkeypatch: pytest.MonkeyPatch,
    with_credentials: None,
    no_sleep: list[float],
) -> None:
    """An auth failure -> valid=False with a message that contains no secret."""
    client = FakeCcxt()
    client.raise_on["fetch_balance"] = ccxt_error("auth", leak=True)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        status = await adapter.validate_credentials()
        assert status.configured is True
        assert status.valid is False
        assert status.can_trade is False
        assert_no_secret(status.message)
        assert SECRET_TOKEN not in status.message


async def test_credential_status_never_serializes_secret(
    monkeypatch: pytest.MonkeyPatch,
    with_credentials: None,
    no_sleep: list[float],
) -> None:
    """Defense in depth: the dumped model JSON carries no secret material."""
    client = FakeCcxt()
    client.raise_on["fetch_balance"] = ccxt_error("auth", leak=True)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
    adapter = await ExchangeAdapter.create("bybit")
    async with adapter:
        status = await adapter.validate_credentials()
        assert_no_secret(status.model_dump_json())
