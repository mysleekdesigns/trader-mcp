"""Typed configuration & secret loading for trader-mcp.

Precedence (highest first): process environment -> ``.env`` file -> OS keyring.
Built on Pydantic v2 + ``pydantic-settings``. Secret values are wrapped in
:class:`pydantic.SecretStr` so they are never rendered by ``repr``/logging.

INVARIANT (safety): this module is the single, isolated home for secret handling.
``risk-safety-engineer`` owns and will harden the keyring path; keep all
secret-resolution logic here so it remains auditable. Nothing in this module logs
or prints a secret value.

The four target exchanges (Coinbase, Kraken, Gemini, Crypto.com) each have an
optional read-only API key/secret pair. Phase 0 only loads and types them; scope
detection and validation land in Phase 1.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trader_mcp.errors import ConfigError

#: Supported exchange identifiers (CCXT ids). Coinbase is the reference exchange.
ExchangeId = Literal["coinbase", "kraken", "gemini", "cryptocom"]


class KeyScope(StrEnum):
    """Declared capability of an exchange credential.

    SAFETY INVARIANT: credentials default to :attr:`READ_ONLY`. A read-only
    credential must be structurally incapable of placing/canceling orders. Phase 1
    (``exchange-adapter-engineer``) enforces this at the adapter boundary -- a
    read-only credential is wired only into market-data/read paths and never into
    order-routing paths. Promotion to :attr:`TRADE_ENABLED` is an explicit,
    deliberate act and remains gated behind ``arm_live_trading`` (Phase 6).
    """

    READ_ONLY = "read_only"
    TRADE_ENABLED = "trade_enabled"


def keyring_lookup(service: str, username: str) -> str | None:
    """Look up a secret in the OS keyring.

    This is a structurally-correct Phase 0 stub: it returns ``None`` (no keyring
    entry) unless the optional ``keyring`` package is installed, in which case it
    delegates to it. ``risk-safety-engineer`` will own/extend this in a follow-up.

    The lookup never logs the requested or returned value.

    Args:
        service: Keyring service name, e.g. ``"trader-mcp:coinbase"``.
        username: Logical key name, e.g. ``"api_key"``.

    Returns:
        The stored secret string, or ``None`` if not found / keyring unavailable.
    """
    try:
        import keyring  # type: ignore[import-not-found]  # optional dependency
    except ImportError:
        return None
    try:
        return keyring.get_password(service, username)
    except Exception:
        return None


class ExchangeCredentials(BaseSettings):
    """Optional read-only credentials for a single exchange.

    Loaded from ``<EXCHANGE>_API_KEY`` / ``<EXCHANGE>_API_SECRET`` (e.g.
    ``COINBASE_API_KEY``). Falls back to the OS keyring when not present in the
    environment or ``.env``. All credentials default to read-only scope in v1.

    Secret fields are :class:`SecretStr`, so ``repr``/``str``/``model_dump`` /
    ``model_dump_json`` never expose plaintext. Read the plaintext only at the
    point of use via :meth:`reveal` (or ``field.get_secret_value()``).
    """

    model_config = SettingsConfigDict(extra="ignore")

    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None

    #: Declared capability of this credential. Defaults to read-only (safe by
    #: default); promotion to trade-enabled is explicit and Phase-6 gated.
    scope: KeyScope = KeyScope.READ_ONLY

    @property
    def is_configured(self) -> bool:
        """True when both an API key and secret are present."""
        return self.api_key is not None and self.api_secret is not None

    @property
    def can_trade(self) -> bool:
        """True only if this credential is *declared* trade-enabled.

        This is a declaration, not an authorization: actually placing a live
        order additionally requires the ``arm_live_trading`` gate (Phase 6). A
        read-only credential returns ``False`` and must never reach an
        order-routing path.
        """
        return self.scope is KeyScope.TRADE_ENABLED

    def reveal(self) -> tuple[str, str]:
        """Return ``(api_key, api_secret)`` plaintext for use at the call site.

        The single auditable place plaintext is unwrapped. Never log or return
        the result over MCP.

        Raises:
            ConfigError: if the credential is not fully configured.
        """
        if self.api_key is None or self.api_secret is None:
            raise ConfigError("Exchange credential is not configured")
        return self.api_key.get_secret_value(), self.api_secret.get_secret_value()


class Settings(BaseSettings):
    """Top-level, typed application settings.

    Resolution precedence is environment > ``.env`` > keyring. Secret fields use
    :class:`SecretStr`; their plaintext is only available via ``.get_secret_value()``
    at the point of use and is never emitted by logging or ``repr``.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADER_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    #: Logging verbosity; also read directly by :mod:`trader_mcp.logging_config`.
    log_level: str = Field(default="INFO")

    #: Default exchange used when a tool call omits one.
    default_exchange: ExchangeId = Field(default="coinbase")

    #: Root directory for the file-based OHLCV cache (DuckDB + Parquet, PRD §5.1).
    #: Project-local by default; override with ``TRADER_MCP_DATA_DIR``. The data
    #: pipeline writes one Parquet file per (exchange, symbol, timeframe) dataset
    #: under this tree. Everything here is gitignored -- never commit data files.
    data_dir: Path = Field(default=Path(".trader_mcp_data"))

    #: Per-request timeout (milliseconds) handed to the CCXT client. Applies to
    #: every exchange call; a generous default tolerates slow public endpoints.
    request_timeout_ms: int = Field(default=30_000, gt=0)

    #: Optional HTTPS proxy URL for CCXT (env ``TRADER_MCP_HTTPS_PROXY``). Lets a
    #: geo-blocked host reach the exchanges. A proxy URL may embed ``user:pass@``,
    #: so it is a :class:`SecretStr`: never logged, revealed only at the CCXT
    #: construction call site in :func:`trader_mcp.exchanges.adapter._apply_network_settings`.
    https_proxy: SecretStr | None = None

    #: Optional SOCKS proxy URL for CCXT (env ``TRADER_MCP_SOCKS_PROXY``). Mutually
    #: exclusive with :attr:`https_proxy`; requires the optional ``aiohttp_socks``
    #: dependency (``uv sync --extra socks``). Also a :class:`SecretStr`.
    socks_proxy: SecretStr | None = None

    @field_validator("https_proxy", "socks_proxy", mode="before")
    @classmethod
    def _empty_proxy_to_none(cls, value: object) -> object:
        """Treat an empty/whitespace-only proxy value as unset (``None``).

        An undefined env var that still expands to ``""`` (e.g. an unset GitHub
        Actions secret referenced in a workflow) must not inject an empty proxy into
        the CCXT client. Runs before ``SecretStr`` coercion.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def credentials_for(self, exchange: ExchangeId) -> ExchangeCredentials:
        """Resolve read-only credentials for ``exchange`` across all sources.

        Reads ``<EXCHANGE>_API_KEY`` / ``<EXCHANGE>_API_SECRET`` from the
        environment and ``.env`` (via ``pydantic-settings``), then fills any gaps
        from the OS keyring. Returns an :class:`ExchangeCredentials` (possibly
        unconfigured); never raises for a missing key.
        """
        env_prefix = f"{exchange.upper()}_"
        creds = ExchangeCredentials(
            _env_prefix=env_prefix,  # type: ignore[call-arg]
        )
        if creds.api_key is None:
            value = keyring_lookup(f"trader-mcp:{exchange}", "api_key")
            if value is not None:
                creds.api_key = SecretStr(value)
        if creds.api_secret is None:
            value = keyring_lookup(f"trader-mcp:{exchange}", "api_secret")
            if value is not None:
                creds.api_secret = SecretStr(value)
        return creds


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, loaded once and cached.

    Raises:
        ConfigError: If the environment/``.env`` contain values that fail typing
            (e.g. an unsupported ``TRADER_MCP_DEFAULT_EXCHANGE``).
    """
    try:
        return Settings()
    except Exception as exc:
        # Defense-in-depth: Settings carries no secret fields today, but never let
        # a validation message echo secret-shaped input if that changes.
        from trader_mcp.logging_config import redact

        raise ConfigError(f"Failed to load settings: {redact(str(exc))}") from exc
