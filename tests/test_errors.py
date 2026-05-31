"""Tests for the error taxonomy and the MCP error-mapping boundary.

Every typed :class:`TraderMCPError` subclass must map to a stable ``code`` and a
clean, JSON-serializable payload; unknown exceptions degrade to a generic
``internal_error``. The payload must never carry stack traces or secret material.
"""

from __future__ import annotations

import json

import pytest

from trader_mcp.errors import (
    ConfigError,
    ExchangeError,
    SafetyError,
    TraderMCPError,
    ValidationError,
    error_to_mcp,
)

# Each known subclass and the stable code it must emit.
_SUBCLASS_CODES = [
    (ConfigError, "config_error"),
    (ValidationError, "validation_error"),
    (ExchangeError, "exchange_error"),
    (SafetyError, "safety_error"),
]


@pytest.mark.parametrize(
    ("cls", "code"),
    _SUBCLASS_CODES,
    ids=[c.__name__ for c, _ in _SUBCLASS_CODES],
)
def test_subclass_maps_to_stable_code(cls: type[TraderMCPError], code: str) -> None:
    assert issubclass(cls, TraderMCPError)
    payload = error_to_mcp(cls("something failed", details={"k": "v"}))
    assert payload == {
        "error": True,
        "code": code,
        "message": "something failed",
        "details": {"k": "v"},
    }


def test_base_error_default_code_and_details() -> None:
    err = TraderMCPError("base message")
    assert err.code == "trader_mcp_error"
    assert err.details == {}
    payload = error_to_mcp(err)
    assert payload["code"] == "trader_mcp_error"
    assert payload["details"] == {}


def test_error_to_mcp_unknown_error_is_generic() -> None:
    payload = error_to_mcp(ValueError("boom"))
    assert payload["error"] is True
    assert payload["code"] == "internal_error"
    assert payload["message"] == "boom"
    assert payload["details"] == {}


def test_error_to_mcp_unknown_error_without_message_uses_class_name() -> None:
    payload = error_to_mcp(RuntimeError())
    assert payload["message"] == "RuntimeError"


def test_error_payload_is_json_serializable() -> None:
    """The boundary payload must serialize cleanly for an MCP client."""
    payload = error_to_mcp(SafetyError("dry-run only", details={"armed": False}))
    encoded = json.loads(json.dumps(payload))
    assert encoded["code"] == "safety_error"


def test_error_payload_has_no_traceback_or_extra_keys() -> None:
    """Payload exposes only the four contract keys -- no stack traces leak."""
    try:
        raise ExchangeError("rejected", details={"http_status": 429})
    except ExchangeError as exc:
        payload = error_to_mcp(exc)
    assert set(payload) == {"error", "code", "message", "details"}
    assert "traceback" not in json.dumps(payload).lower()
