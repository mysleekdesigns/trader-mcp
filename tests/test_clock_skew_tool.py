"""Tests for the Phase 7 ``check_clock_skew`` connectivity/admin MCP tool.

This tool wraps the read-only, unauthenticated resilience contract
:meth:`trader_mcp.exchanges.ExchangeAdapter.check_clock_skew` and returns a typed
:class:`~trader_mcp.exchanges.ClockSkew` model, so the structured tool output must
validate against that model. The single offline seam is the same one the adapter
and market-data tool tests use:
``trader_mcp.exchanges.adapter._create_ccxt_client`` is monkeypatched to a fake --
no real network is touched, and no credentials are required (the skew check is a
public ``fetch_time`` read).
"""

from __future__ import annotations

from typing import Any

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import FakeCcxt, make_create_factory
from trader_mcp.exchanges import ClockSkew
from trader_mcp.server.app import build_app


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-output dict from a FastMCP ``call_tool`` result."""
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


async def _tools_by_name(app: Any) -> dict[str, Any]:
    return {t.name: t for t in await app.list_tools()}


@pytest.fixture
def app_with_fake(monkeypatch: pytest.MonkeyPatch, no_credentials: None) -> Any:
    """A built app whose adapters wrap a fresh fake CCXT client per exchange."""
    clients = [FakeCcxt() for _ in range(4)]
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(clients=clients))
    return build_app()


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
async def test_check_clock_skew_registered() -> None:
    """The tool is registered as part of the connectivity/admin group."""
    app = build_app()
    names = set(await _tools_by_name(app))
    assert "check_clock_skew" in names


async def test_check_clock_skew_advertises_structured_output_schema() -> None:
    app = build_app()
    tool = (await _tools_by_name(app))["check_clock_skew"]
    assert tool.outputSchema is not None
    assert tool.outputSchema.get("type") == "object"


async def test_check_clock_skew_constrains_exchange_enum() -> None:
    """``exchange`` is constrained to the four supported ids at the schema level."""
    app = build_app()
    tool = (await _tools_by_name(app))["check_clock_skew"]
    props = tool.inputSchema["properties"]
    assert set(props["exchange"]["enum"]) == {"coinbase", "kraken", "gemini", "cryptocom"}


# --------------------------------------------------------------------------- #
# Behavior: structured output validates against ClockSkew
# --------------------------------------------------------------------------- #
async def test_check_clock_skew_returns_typed_clockskew(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool("check_clock_skew", {"exchange": "coinbase"})
    )
    skew = ClockSkew.model_validate(structured)
    assert skew.exchange == "coinbase"
    assert skew.threshold_ms == 1000.0
    # ``within_tolerance`` is derived from abs(skew_ms) vs the threshold.
    assert skew.within_tolerance == (abs(skew.skew_ms) <= skew.threshold_ms)


async def test_check_clock_skew_within_tolerance_with_wide_threshold(
    monkeypatch: pytest.MonkeyPatch, no_credentials: None
) -> None:
    """A server clock matching local (small skew) is within a wide tolerance."""
    import time as _time

    client = FakeCcxt()
    # Align the fake exchange server time with the real local clock so the measured
    # skew is tiny; a small explicit threshold then comfortably contains it.
    client.server_time_ms = int(_time.time() * 1000)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
    app = build_app()
    structured = _structured(
        await app.call_tool("check_clock_skew", {"exchange": "coinbase", "threshold_ms": 5000.0})
    )
    skew = ClockSkew.model_validate(structured)
    assert skew.within_tolerance is True
    assert abs(skew.skew_ms) <= skew.threshold_ms


async def test_check_clock_skew_flags_drift_past_threshold(
    monkeypatch: pytest.MonkeyPatch, no_credentials: None
) -> None:
    """A server clock far from local marks the result out of tolerance."""
    client = FakeCcxt()
    # Push the exchange server time 10s behind the local clock.
    client.server_time_ms -= 10_000
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
    app = build_app()
    structured = _structured(
        await app.call_tool("check_clock_skew", {"exchange": "coinbase", "threshold_ms": 1000.0})
    )
    skew = ClockSkew.model_validate(structured)
    assert skew.within_tolerance is False
    assert abs(skew.skew_ms) > skew.threshold_ms


async def test_check_clock_skew_honors_custom_threshold(app_with_fake: Any) -> None:
    """An explicit threshold flows through to the typed result."""
    structured = _structured(
        await app_with_fake.call_tool(
            "check_clock_skew", {"exchange": "coinbase", "threshold_ms": 5.0}
        )
    )
    skew = ClockSkew.model_validate(structured)
    assert skew.threshold_ms == 5.0


async def test_check_clock_skew_rejects_unknown_exchange(app_with_fake: Any) -> None:
    """An unsupported exchange is rejected at the typed MCP boundary."""
    with pytest.raises(Exception):  # noqa: B017, PT011 - error type lives behind the SDK
        await app_with_fake.call_tool("check_clock_skew", {"exchange": "binance"})
