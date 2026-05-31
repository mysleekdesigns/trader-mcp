"""Shared pytest fixtures for the trader-mcp test suite.

``qa-parity-engineer`` expands this with the backtest<->live parity harness and
recorded-fixture integration in later phases.

Phase 1 fixtures keep the exchange + market-data suite fully offline: they
monkeypatch the single client-construction seam
(``trader_mcp.exchanges.adapter._create_ccxt_client``) to return a deterministic
:class:`tests._fakes.FakeCcxt`, and patch ``_sleep`` to a no-op so retry/backoff
tests never actually sleep.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FakeCcxt, make_create_factory
from trader_mcp.config import Settings, get_settings


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--live``, the opt-in flag for real-exchange network tests."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run @pytest.mark.live tests (real exchange network access).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``live``-marked tests unless --live or ``TRADER_MCP_LIVE_TESTS=1``.

    Keeps the default ``uv run pytest`` fully offline and deterministic: the live
    suite only runs when explicitly opted into (over a VPN/proxy, or in the
    dedicated live-validation CI job).
    """
    if config.getoption("--live") or os.environ.get("TRADER_MCP_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="needs real exchange network access; pass --live or set TRADER_MCP_LIVE_TESTS=1"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def clean_settings_cache() -> None:
    """Clear the cached :func:`get_settings` result before a test.

    Lets a test mutate the environment and observe a fresh ``Settings`` load.
    """
    get_settings.cache_clear()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the adapter's backoff ``_sleep`` with an awaited no-op.

    Returns the list of requested delays so tests can assert backoff was awaited
    (and how many times) without any real wall-clock wait.
    """
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(adapter_module, "_sleep", fake_sleep)
    return delays


@pytest.fixture
def patch_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., FakeCcxt]:
    """Install a :class:`FakeCcxt` behind ``_create_ccxt_client`` and return it.

    Usage::

        client = patch_client()  # default fake
        client = patch_client(has={...})  # customized fake

    Any keyword args are forwarded to :class:`FakeCcxt`. The returned client is
    the exact instance the next ``ExchangeAdapter.create`` / ``ExchangeManager.get``
    will wrap, so tests can poke its knobs and inspect call counters.
    """

    def install(**kwargs: object) -> FakeCcxt:
        client = FakeCcxt(**kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
        return client

    return install


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no exchange credentials resolve, regardless of the host env/.env.

    Pins ``Settings.credentials_for`` to an unconfigured credential so
    ``ExchangeAdapter.create`` wires no key/secret and ``validate_credentials``
    takes the no-network path.
    """
    from trader_mcp.config import ExchangeCredentials

    def empty(self: Settings, exchange: str) -> ExchangeCredentials:
        return ExchangeCredentials(api_key=None, api_secret=None)

    monkeypatch.setattr(Settings, "credentials_for", empty)
    get_settings.cache_clear()
