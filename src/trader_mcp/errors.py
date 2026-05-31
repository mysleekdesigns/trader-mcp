"""Error taxonomy and MCP error mapping for trader-mcp.

Internal code raises typed :class:`TraderMCPError` subclasses. At the MCP boundary
those are converted with :func:`error_to_mcp` into clean, structured payloads that
an AI client can act on -- never leaking stack traces or secrets.
"""

from __future__ import annotations

from typing import Any

# NOTE: imported lazily inside error_to_mcp to avoid a circular import
# (logging_config imports nothing from here, but config imports from here).


class TraderMCPError(Exception):
    """Base class for all trader-mcp errors.

    Attributes:
        message: Human-readable description (AI-friendly, no secrets).
        code: Stable machine-readable error code (defaults to the class name).
        details: Optional structured context safe to surface to the client.
    """

    #: Stable error code used in MCP responses; overridden per subclass.
    code: str = "trader_mcp_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(TraderMCPError):
    """Configuration or secret-loading failure (missing/invalid settings)."""

    code = "config_error"


class ValidationError(TraderMCPError):
    """A tool input or strategy spec failed validation."""

    code = "validation_error"


class ExchangeError(TraderMCPError):
    """An exchange/CCXT operation failed (connectivity, rate limit, rejection)."""

    code = "exchange_error"


class SafetyError(TraderMCPError):
    """A safety guardrail blocked an operation (dry-run, unarmed, risk limit)."""

    code = "safety_error"


def error_to_mcp(error: Exception) -> dict[str, Any]:
    """Convert any exception into a clean, structured MCP error payload.

    Known :class:`TraderMCPError` subclasses map to their stable ``code`` and
    surface their ``details``. Unknown exceptions are mapped to a generic
    ``internal_error`` with only their message -- callers should rely on logging
    (which is redacted) for diagnostics rather than the wire payload.

    SAFETY: every string that crosses the MCP boundary here is passed through the
    secret-redaction filter, so even an unexpected exception raised by a third
    party (e.g. a CCXT auth error that embeds an API key, or a pydantic
    ``ValidationError`` echoing raw input) cannot leak secret material to the
    client. ``details`` values are scrubbed recursively.

    Args:
        error: The exception raised somewhere below the MCP boundary.

    Returns:
        A JSON-serializable dict with ``error`` (bool), ``code`` (str),
        ``message`` (str), and ``details`` (dict).
    """
    # Lazy import keeps the module import graph acyclic and stdlib-light.
    from trader_mcp.logging_config import redact

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(scrub(v) for v in value)
        return value

    if isinstance(error, TraderMCPError):
        return {
            "error": True,
            "code": error.code,
            "message": redact(error.message),
            "details": scrub(error.details),
        }
    return {
        "error": True,
        "code": "internal_error",
        "message": redact(str(error)) or error.__class__.__name__,
        "details": {},
    }
