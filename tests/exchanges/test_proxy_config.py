"""Offline tests for proxy + timeout threading into the CCXT client config.

These prove the geo-block escape hatch (PRD §6 Phase 1 live-validation) without any
network: the proxy/timeout settings must land in the dict handed to
``_create_ccxt_client``, both-proxies-set must be rejected, a SOCKS proxy without the
optional ``aiohttp_socks`` dependency must be rejected, and a proxy URL (which may
embed credentials) must never appear in logs.

The capture seam is the same one the whole suite uses: monkeypatch
``trader_mcp.exchanges.adapter._create_ccxt_client``. Here the replacement records the
config it is handed instead of returning a canned fake's payloads.
"""

from __future__ import annotations

import logging
from typing import Any

import ccxt
import pytest
from pydantic import SecretStr

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FakeCcxt
from trader_mcp.config import Settings
from trader_mcp.errors import ConfigError
from trader_mcp.exchanges import ExchangeAdapter, map_ccxt_error

# Every test here runs fully offline with no real credentials.
pytestmark = pytest.mark.usefixtures("no_credentials")

#: A realistic base64-charset proxy password (contains ``/ + =``, < 32 chars) used
#: to prove it never leaks. Deliberately NOT a 32+ char alnum token, so redaction
#: must come from the URL-userinfo rule -- not the long-token catch-all -- and the
#: ``/ + =`` exercise the broad password class (a too-narrow class would leak these).
PROXY_TOKEN = "aB3/xY9z+Cg=="


def _capture_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``_create_ccxt_client`` to record (and not use) the config it receives."""
    captured: dict[str, Any] = {}

    def factory(exchange_id: str, config: dict[str, Any]) -> FakeCcxt:
        captured["exchange_id"] = exchange_id
        captured["config"] = config
        return FakeCcxt()

    monkeypatch.setattr(adapter_module, "_create_ccxt_client", factory)
    return captured


async def test_timeout_always_lands_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_config(monkeypatch)
    adapter = await ExchangeAdapter.create("coinbase", settings=Settings(request_timeout_ms=12_345))
    async with adapter:
        assert captured["config"]["timeout"] == 12_345


async def test_no_proxy_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_config(monkeypatch)
    adapter = await ExchangeAdapter.create("coinbase", settings=Settings())
    async with adapter:
        config = captured["config"]
        assert "httpsProxy" not in config
        assert "socksProxy" not in config
        assert config["timeout"] == 30_000  # documented default


async def test_empty_string_proxies_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty/whitespace proxy env vars (e.g. an unset CI secret) must be ignored.

    A GitHub Actions ``${{ secrets.X }}`` for an undefined secret expands to ``""``;
    that must neither trip the both-proxies guard nor inject an empty proxy.
    """
    monkeypatch.setenv("TRADER_MCP_HTTPS_PROXY", "")
    monkeypatch.setenv("TRADER_MCP_SOCKS_PROXY", "   ")
    captured = _capture_config(monkeypatch)
    adapter = await ExchangeAdapter.create("coinbase", settings=Settings())
    async with adapter:
        config = captured["config"]
        assert "httpsProxy" not in config
        assert "socksProxy" not in config


async def test_https_proxy_lands_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_config(monkeypatch)
    url = f"http://user:{PROXY_TOKEN}@proxy.example:8080"
    adapter = await ExchangeAdapter.create(
        "coinbase", settings=Settings(https_proxy=SecretStr(url))
    )
    async with adapter:
        config = captured["config"]
        assert config["httpsProxy"] == url
        assert "socksProxy" not in config


async def test_socks_proxy_lands_in_config_when_dep_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend the optional dependency is installed so the SOCKS path is exercised.
    monkeypatch.setattr(adapter_module, "_aiohttp_socks_available", lambda: True)
    captured = _capture_config(monkeypatch)
    url = f"socks5://user:{PROXY_TOKEN}@proxy.example:1080"
    adapter = await ExchangeAdapter.create(
        "coinbase", settings=Settings(socks_proxy=SecretStr(url))
    )
    async with adapter:
        config = captured["config"]
        assert config["socksProxy"] == url
        assert "httpsProxy" not in config


async def test_both_proxies_set_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_config(monkeypatch)
    settings = Settings(
        https_proxy=SecretStr("http://proxy.example:8080"),
        socks_proxy=SecretStr("socks5://proxy.example:1080"),
    )
    with pytest.raises(ConfigError, match="only one"):
        await ExchangeAdapter.create("coinbase", settings=settings)


async def test_socks_proxy_without_dependency_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "_aiohttp_socks_available", lambda: False)
    _capture_config(monkeypatch)
    settings = Settings(socks_proxy=SecretStr("socks5://proxy.example:1080"))
    with pytest.raises(ConfigError, match="aiohttp_socks"):
        await ExchangeAdapter.create("coinbase", settings=settings)


async def test_proxy_url_never_appears_in_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No log call during create() may ever receive the proxy credential.

    The capturing handler is attached directly to the ``trader_mcp`` logger and the
    messages are read BEFORE the package handler's ``RedactionFilter`` runs, so this
    asserts the stronger property that the plaintext credential is never even passed
    to a log call (not merely that it would be scrubbed if it were).
    """
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture()
    root = logging.getLogger("trader_mcp")
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        _capture_config(monkeypatch)
        url = f"http://user:{PROXY_TOKEN}@proxy.example:8080"
        adapter = await ExchangeAdapter.create(
            "coinbase", settings=Settings(https_proxy=SecretStr(url))
        )
        await adapter.aclose()
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    assert all(PROXY_TOKEN not in msg for msg in captured), "proxy credential leaked into logs"


def test_proxy_credential_redacted_in_mapped_error() -> None:
    """A proxy URL surfacing in a CCXT error must be scrubbed by ``map_ccxt_error``.

    The realistic leak path: a proxy auth/connection failure whose CCXT message
    embeds the full ``scheme://user:pass@host`` URL. That message becomes
    ``details["cause"]`` (via ``redact``) and flows to logs, the MCP wire payload,
    and the CLI report -- so the credential must be gone from both the mapped error's
    string and its ``details``.
    """
    url = f"http://user:{PROXY_TOKEN}@proxy.example:8080"
    exc = ccxt.NetworkError(f"Cannot connect to proxy {url}: connection refused")
    mapped = map_ccxt_error(exc, exchange="coinbase", op="fetch_ticker")
    assert PROXY_TOKEN not in str(mapped)
    assert PROXY_TOKEN not in mapped.details["cause"]
