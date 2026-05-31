"""Tests for the typed settings/secret loader and its precedence.

Covers: default values, env-over-``.env`` precedence (via a real tmp ``.env``),
optional secrets that may be absent without crashing, keyring fallback, the
ConfigError path, and -- critically -- the secret leak-guard: ``SecretStr`` values
must never appear in ``repr``/``str``/``model_dump``/``model_dump_json``.

Hermetic: no real ``.env`` or keyring is read; everything is monkeypatched or
fed through a tmp file.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from pydantic import SecretStr

from trader_mcp.config import (
    ExchangeCredentials,
    KeyScope,
    Settings,
    get_settings,
    keyring_lookup,
)
from trader_mcp.errors import ConfigError


# --------------------------------------------------------------------------- #
# Defaults & precedence
# --------------------------------------------------------------------------- #
def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADER_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TRADER_MCP_DEFAULT_EXCHANGE", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "INFO"
    assert settings.default_exchange == "bybit"


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_MCP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TRADER_MCP_DEFAULT_EXCHANGE", "blofin")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "DEBUG"
    assert settings.default_exchange == "blofin"


def test_dotenv_file_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Values present only in a ``.env`` file are picked up."""
    monkeypatch.delenv("TRADER_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TRADER_MCP_DEFAULT_EXCHANGE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TRADER_MCP_LOG_LEVEL=WARNING\nTRADER_MCP_DEFAULT_EXCHANGE=toobit\n")

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert settings.log_level == "WARNING"
    assert settings.default_exchange == "toobit"


def test_env_takes_precedence_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Process environment must win over a value in the ``.env`` file."""
    env_file = tmp_path / ".env"
    env_file.write_text("TRADER_MCP_LOG_LEVEL=WARNING\nTRADER_MCP_DEFAULT_EXCHANGE=toobit\n")
    # Env overrides the .env-provided log level but not the exchange.
    monkeypatch.setenv("TRADER_MCP_LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("TRADER_MCP_DEFAULT_EXCHANGE", raising=False)

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert settings.log_level == "DEBUG"  # env wins
    assert settings.default_exchange == "toobit"  # falls back to .env


# --------------------------------------------------------------------------- #
# get_settings caching + error taxonomy
# --------------------------------------------------------------------------- #
def test_get_settings_is_cached(clean_settings_cache: None) -> None:
    assert get_settings() is get_settings()


def test_invalid_exchange_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_MCP_DEFAULT_EXCHANGE", "not_a_real_exchange")
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        get_settings()
    get_settings.cache_clear()


def test_config_error_message_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret-shaped invalid value must not echo back through ConfigError."""
    token = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"  # 32-char opaque token shape
    monkeypatch.setenv("TRADER_MCP_DEFAULT_EXCHANGE", token)
    get_settings.cache_clear()
    with pytest.raises(ConfigError) as excinfo:
        get_settings()
    assert token not in str(excinfo.value)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Credentials resolution
# --------------------------------------------------------------------------- #
def test_credentials_from_env_take_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "env-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "env-secret")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    creds = settings.credentials_for("bybit")
    assert creds.is_configured
    assert creds.api_key is not None
    assert creds.api_key.get_secret_value() == "env-key"


def test_credentials_unconfigured_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing optional secrets must not crash; the credential is just unconfigured."""
    for var in ("WEEX_API_KEY", "WEEX_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    creds = settings.credentials_for("weex")
    assert not creds.is_configured


def test_keyring_fallback_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOOBIT_API_KEY", raising=False)
    monkeypatch.delenv("TOOBIT_API_SECRET", raising=False)

    def fake_keyring(service: str, username: str) -> str | None:
        return f"kr-{username}"

    monkeypatch.setattr("trader_mcp.config.keyring_lookup", fake_keyring)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    creds = settings.credentials_for("toobit")
    assert creds.is_configured
    assert creds.api_key is not None
    assert creds.api_key.get_secret_value() == "kr-api_key"


def test_keyring_lookup_returns_none_when_package_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional ``keyring`` package is not installed -> ImportError branch -> None."""
    monkeypatch.setitem(sys.modules, "keyring", None)  # force ImportError on import
    assert keyring_lookup("trader-mcp:bybit", "api_key") is None


def test_keyring_lookup_delegates_to_installed_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``keyring`` is importable, the stored value is returned."""
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, username: f"stored-{username}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert keyring_lookup("trader-mcp:bybit", "api_key") == "stored-api_key"


def test_keyring_lookup_swallows_backend_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing keyring backend must degrade to ``None``, never raise."""
    fake = types.ModuleType("keyring")

    def boom(service: str, username: str) -> str:
        raise RuntimeError("keyring backend locked")

    fake.get_password = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert keyring_lookup("trader-mcp:bybit", "api_key") is None


def test_env_beats_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both env and keyring have a value, env wins (precedence order)."""
    monkeypatch.setenv("BYBIT_API_KEY", "env-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "env-secret")

    def fake_keyring(service: str, username: str) -> str | None:  # pragma: no cover
        return "keyring-should-not-be-used"

    monkeypatch.setattr("trader_mcp.config.keyring_lookup", fake_keyring)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    creds = settings.credentials_for("bybit")
    assert creds.api_key is not None
    assert creds.api_key.get_secret_value() == "env-key"


# --------------------------------------------------------------------------- #
# Secret leak-guard (SAFETY INVARIANT)
# --------------------------------------------------------------------------- #
def test_secretstr_not_exposed_by_any_serialization() -> None:
    """Plaintext must never appear in repr/str/model_dump/model_dump_json."""
    creds = ExchangeCredentials(
        api_key=SecretStr("plaintext-key-DO-NOT-LEAK"),
        api_secret=SecretStr("plaintext-secret-DO-NOT-LEAK"),
    )
    surfaces = [
        repr(creds),
        str(creds),
        str(creds.model_dump()),
        creds.model_dump_json(),
        str(creds.api_key),
        repr(creds.api_key),
    ]
    for surface in surfaces:
        assert "plaintext-key-DO-NOT-LEAK" not in surface
        assert "plaintext-secret-DO-NOT-LEAK" not in surface


def test_get_secret_value_returns_plaintext() -> None:
    """The only sanctioned plaintext path round-trips correctly."""
    creds = ExchangeCredentials(
        api_key=SecretStr("the-key"),
        api_secret=SecretStr("the-secret"),
    )
    assert creds.api_key is not None
    assert creds.api_key.get_secret_value() == "the-key"


def test_reveal_returns_plaintext_pair() -> None:
    creds = ExchangeCredentials(
        api_key=SecretStr("the-key"),
        api_secret=SecretStr("the-secret"),
    )
    assert creds.reveal() == ("the-key", "the-secret")


def test_reveal_raises_config_error_when_unconfigured() -> None:
    creds = ExchangeCredentials()
    with pytest.raises(ConfigError):
        creds.reveal()


# --------------------------------------------------------------------------- #
# Key scoping (safe-by-default)
# --------------------------------------------------------------------------- #
def test_credentials_default_to_read_only() -> None:
    """SAFETY INVARIANT: a freshly loaded credential is read-only and cannot trade."""
    creds = ExchangeCredentials(api_key=SecretStr("k"), api_secret=SecretStr("s"))
    assert creds.scope is KeyScope.READ_ONLY
    assert creds.can_trade is False


def test_resolved_credentials_are_read_only_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    creds = Settings(_env_file=None).credentials_for("bybit")  # type: ignore[call-arg]
    assert creds.scope is KeyScope.READ_ONLY
    assert creds.can_trade is False
