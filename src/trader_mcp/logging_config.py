"""Structured logging with secret redaction for trader-mcp.

INVARIANT (safety): secrets are radioactive. Every log record passes through
:class:`RedactionFilter`, which scrubs anything resembling an API key, secret,
token, or password from messages and arguments before they are emitted. Modules
should obtain loggers via :func:`get_logger` rather than configuring logging
themselves.

The log level is controlled by the ``TRADER_MCP_LOG_LEVEL`` environment variable
(default ``INFO``). Logging is sent to ``stderr`` so it never corrupts the MCP
stdio transport on ``stdout``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, ClassVar

#: Value substituted in place of any detected secret material.
REDACTION_PLACEHOLDER = "***REDACTED***"

#: Keys whose values are always redacted when they appear as ``key=value`` or
#: ``"key": "value"`` style fragments in a formatted message.
_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "api-key",
    "api_secret",
    "apisecret",
    "secret",
    "password",
    "passphrase",
    "token",
    "private_key",
    # NOTE: "authorization" is intentionally NOT keyed here -- bearer tokens are
    # handled by _BEARER_RE so the scheme word ("Bearer") is not mistaken for the
    # secret value.
)

# Authorization / Proxy-Authorization headers in any scheme (Bearer, Basic, raw
# token, etc.). The scheme word (a single bare word like "Bearer"/"Basic"), if
# present, is preserved; everything after it -- or the whole value when no scheme
# word is present -- is redacted. Covers "Authorization: Bearer abc.def.ghi",
# "Authorization: Basic dXNlcjpwYXNz", and "Authorization=rawtoken". Run before
# the keyed rule so the scheme word is not mistaken for the secret value.
_AUTH_HEADER_RE = re.compile(
    r"(?i)((?:proxy-)?authorization)"  # header name
    r'(["\']?\s*[:=]\s*["\']?)'  # separator
    r"(?:([A-Za-z]+)\s+)?"  # optional scheme word, e.g. Bearer / Basic
    r"(?!\*\*\*REDACTED)[^\s,;\"'}\)]+"  # the credential material itself
)

# Bare bearer tokens (no Authorization prefix), e.g. "Bearer abc.def.ghi".
# The capture keeps the leading "bearer " keyword. Any *further* "bearer "
# keywords are consumed (not captured) before the real token, so a doubled shape
# like "bearer Bearer <token>" -- which arises when a template already says
# "bearer" and the arg is itself a full "Bearer <token>" -- redacts the real
# token instead of mistaking the second scheme word for the secret value.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)(?:bearer\s+)*(?!\*\*\*REDACTED)[A-Za-z0-9._\-]+")

# key=value  /  key: value  /  "key": "value"  (value captured in group "val").
# The value stops at the placeholder so an already-redacted value is left alone.
_KEYED_SECRET_RE = re.compile(
    r"(?i)(?P<key>" + "|".join(re.escape(k) for k in _SENSITIVE_KEYS) + r")"
    r'(?P<sep>["\']?\s*[:=]\s*["\']?)'
    r"(?P<val>(?!\*\*\*REDACTED)[^\s,;\"'}\)&]+)"
)

# URL-embedded credentials: ``scheme://user:pass@host`` -> ``scheme://***@host``.
# A proxy URL (TRADER_MCP_HTTPS_PROXY/SOCKS_PROXY) may embed ``user:pass@`` and can
# surface verbatim in a CCXT proxy/connection error message. The whole userinfo is
# redacted (the username can be sensitive too). The password class is deliberately
# broad -- ``[^\s@]*`` matches everything up to the ``@`` terminator, including the
# ``/ + =`` of a base64 secret -- because we must never enumerate the charset and
# leave a gap; a too-narrow class is exactly how a base64 password would slip past.
# Over-redacting a contrived ``scheme://x:y/z@w`` log line is acceptable; leaking a
# credential is not. Runs first so the full userinfo collapses before other rules
# nibble at it, and is idempotent (the placeholder has no ``:`` before the ``@``).
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s@/]+:[^\s@]*@")

# Long opaque high-entropy-looking tokens (>= 32 chars of base64/hex-ish text).
# This is the catch-all for the longer secrets the four target exchanges issue
# (Coinbase/Kraken/Gemini/Crypto.com API *secrets* are typically >= 32 chars). Shorter
# bare API *keys* are caught only when they appear next to a sensitive key name
# or auth header above; the structural defense for those is :class:`SecretStr`.
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _redact_auth_header(match: re.Match[str]) -> str:
    name, sep, scheme = match.group(1), match.group(2), match.group(3)
    if scheme:
        return f"{name}{sep}{scheme} {REDACTION_PLACEHOLDER}"
    return f"{name}{sep}{REDACTION_PLACEHOLDER}"


def redact(text: str) -> str:
    """Return ``text`` with any detected secret material replaced.

    Applies, in order: URL-embedded credentials (``scheme://user:pass@``),
    ``Authorization`` headers, bare bearer tokens, keyed secrets (``api_key=...``),
    then long opaque tokens. Safe to call on arbitrary strings; the placeholder is
    never re-redacted on a second pass.
    """
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTION_PLACEHOLDER}@", text)
    text = _AUTH_HEADER_RE.sub(_redact_auth_header, text)
    text = _BEARER_RE.sub(rf"\1{REDACTION_PLACEHOLDER}", text)
    text = _KEYED_SECRET_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACTION_PLACEHOLDER}",
        text,
    )
    text = _LONG_TOKEN_RE.sub(REDACTION_PLACEHOLDER, text)
    return text


class RedactionFilter(logging.Filter):
    """Logging filter that scrubs secrets from messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Render the final message (merging %-args) and redact that, so secrets are
        # scrubbed whether they live in the template, the args, or only become a
        # "key=value" pair once formatted. Args are then cleared because the
        # rendered text is now stored directly in record.msg.
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            # Malformed format string/args (e.g. arg-count mismatch). We must not
            # re-raise here -- and must not leave the bad template+args in place,
            # or the formatter's own getMessage() would re-raise during emission
            # and could surface unredacted args. Redact every part and collapse
            # to a single safe, already-rendered string with args cleared.
            redacted_args = self._redact_args(record.args) if record.args else record.args
            # Final pass over the composed string in case a non-str arg's repr()
            # surfaced secret-shaped material that _redact_args could not scrub.
            record.msg = redact(f"{record.msg!s} [unformattable args: {redacted_args!r}]")
            record.args = None
            return True
        record.msg = redact(rendered)
        record.args = None
        return True

    @staticmethod
    def _redact_args(args: Any) -> Any:
        def scrub(value: Any) -> Any:
            return redact(value) if isinstance(value, str) else value

        if isinstance(args, dict):
            return {k: scrub(v) for k, v in args.items()}
        if isinstance(args, tuple):
            return tuple(scrub(v) for v in args)
        return scrub(args)


class _Configurator:
    """Idempotent one-time logging configuration for the package root logger."""

    _LOGGER_NAME: ClassVar[str] = "trader_mcp"
    _configured: ClassVar[bool] = False

    @classmethod
    def configure(cls) -> None:
        if cls._configured:
            return
        level_name = os.environ.get("TRADER_MCP_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

        logger = logging.getLogger(cls._LOGGER_NAME)
        logger.setLevel(level)
        logger.propagate = False

        # stderr so MCP stdio (stdout) stays a clean JSON-RPC channel.
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        handler.addFilter(RedactionFilter())
        logger.addHandler(handler)

        cls._configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a redaction-protected logger for ``name``.

    Configures the package root logger once (level from ``TRADER_MCP_LOG_LEVEL``),
    then returns a child logger. The returned logger inherits the redaction filter
    via the package root handler.

    Args:
        name: Usually ``__name__`` of the calling module.

    Returns:
        A :class:`logging.Logger` whose output is routed through the redacting
        package handler on ``stderr``.
    """
    _Configurator.configure()
    if name == "trader_mcp" or name.startswith("trader_mcp."):
        return logging.getLogger(name)
    return logging.getLogger(f"trader_mcp.{name}")
