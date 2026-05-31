"""Tests for the secret-redaction logging filter.

INVARIANT (PRD §5.4): secrets are radioactive -- every log record passes through
:class:`RedactionFilter` before emission. These tests prove the filter scrubs
API keys, secrets, bearer tokens, and long opaque tokens from the message
template, from ``%``-style positional args, and from mapping args, while leaving
secret-free text untouched.
"""

from __future__ import annotations

import logging

import pytest

from trader_mcp.logging_config import (
    REDACTION_PLACEHOLDER,
    RedactionFilter,
    get_logger,
    redact,
)

# (label, input, secret-that-must-vanish, expect_placeholder)
_REDACT_CASES = [
    ("api_key_eq", "api_key=supersecretvalue", "supersecretvalue", True),
    ("api_secret_json", '{"api_secret": "topsecretvalue"}', "topsecretvalue", True),
    ("password_colon", "password: hunter2password", "hunter2password", True),
    ("passphrase", "passphrase=correct-horse-battery", "correct-horse-battery", True),
    ("token_kv", "token=abc123def456ghi789", "abc123def456ghi789", True),
    ("bearer", "Authorization: Bearer abc.def.ghijklmnop", "abc.def.ghijklmnop", True),
    ("bare_bearer", "sent Bearer abc.def.ghijklmnop now", "abc.def.ghijklmnop", True),
    # Doubled shape: template says "bearer" and the arg is itself "Bearer <tok>",
    # rendering to "...bearer Bearer <tok>". The second scheme word must not be
    # mistaken for the secret -- the real token must still vanish.
    ("doubled_bearer", "auth via bearer Bearer myRealToken123", "myRealToken123", True),
    ("auth_basic", "Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA==", True),
    ("auth_raw_token", "Authorization=rawtokensecret999", "rawtokensecret999", True),
    (
        "proxy_auth",
        "Proxy-Authorization: Bearer proxytoken.value.here",
        "proxytoken.value.here",
        True,
    ),
    (
        "long_opaque",
        "got A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6 now",
        "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
        True,
    ),
    # Proxy URLs (TRADER_MCP_HTTPS_PROXY/SOCKS_PROXY) may embed user:pass@ and can
    # surface in a CCXT proxy/connection error. The whole userinfo must be scrubbed
    # regardless of password length or special chars (short pw would slip past the
    # >= 32-char long-token rule).
    (
        "proxy_url_short_pw",
        "connect via http://user:hunter2@proxy.example:8080 failed",
        "hunter2",
        True,
    ),
    (
        "proxy_url_special_pw",
        "proxy error: socks5://alice:p4ss-w0rd.x@10.0.0.1:1080 refused",
        "p4ss-w0rd.x",
        True,
    ),
    # Base64 secrets routinely contain / + = -- the password class MUST cover them,
    # or the credential slips past the rule unredacted.
    (
        "proxy_url_base64_pw",
        "connect via http://user:aB3/xY9z+Cg==@proxy.example:8080 refused",
        "aB3/xY9z+Cg==",
        True,
    ),
    # A proxy URL with NO credentials carries no secret -> must pass through intact.
    ("proxy_url_no_creds", "using http://proxy.example:8080 now", None, False),
    ("safe_text", "fetching BTC/USDT ticker on bybit", None, False),
    ("short_id", "order id ABC123 placed", None, False),
]


@pytest.mark.parametrize(
    ("label", "text", "secret", "expect_placeholder"),
    _REDACT_CASES,
    ids=[c[0] for c in _REDACT_CASES],
)
def test_redact_table(label: str, text: str, secret: str | None, expect_placeholder: bool) -> None:
    out = redact(text)
    if secret is not None:
        assert secret not in out, f"{label}: secret leaked"
    if expect_placeholder:
        assert REDACTION_PLACEHOLDER in out, f"{label}: expected redaction placeholder"
    else:
        # Secret-free text must pass through byte-for-byte.
        assert out == text, f"{label}: safe text was altered"


def test_redact_is_idempotent() -> None:
    """Re-running redaction must not double-scrub an already-redacted value."""
    once = redact("api_key=supersecretvalue")
    assert redact(once) == once


def test_redact_proxy_url_is_idempotent() -> None:
    """A redacted proxy URL must survive a second redaction pass unchanged."""
    once = redact("http://user:hunter2@proxy.example:8080")
    assert "hunter2" not in once
    assert "user" not in once.split("@")[0].split("//")[1]  # userinfo collapsed
    assert redact(once) == once


# --------------------------------------------------------------------------- #
# Filter on real LogRecords (template, %-args, mapping-args, fallback)
# --------------------------------------------------------------------------- #
def _make_record(msg: str, args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="trader_mcp.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,  # type: ignore[arg-type]
        exc_info=None,
    )


def test_filter_scrubs_secret_in_positional_args() -> None:
    """Secret lives only in a ``%s`` arg, key only in the template."""
    record = _make_record("connecting with api_key=%s", ("leakysecretkey1234",))
    assert RedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "leakysecretkey1234" not in rendered
    assert REDACTION_PLACEHOLDER in rendered
    # Args are consumed into the rendered message, so re-rendering is stable.
    assert "leakysecretkey1234" not in record.getMessage()


def test_filter_scrubs_secret_in_mapping_args() -> None:
    """A ``%(name)s`` mapping arg carrying a secret is scrubbed end-to-end."""
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("trader_mcp.test.mapping")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _Capture()
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    try:
        logger.info("connecting api_key=%(k)s", {"k": "supersecretvalue12345678"})
    finally:
        logger.removeHandler(handler)

    assert captured, "handler did not emit"
    assert "supersecretvalue12345678" not in captured[-1]
    assert REDACTION_PLACEHOLDER in captured[-1]


def test_filter_passes_through_clean_message_unchanged() -> None:
    """A record with no secret material renders to its plain formatted text."""
    record = _make_record("fetched %d candles for %s", (500, "BTC/USDT"))
    assert RedactionFilter().filter(record) is True
    assert record.getMessage() == "fetched 500 candles for BTC/USDT"


def test_filter_handles_malformed_format_without_leaking() -> None:
    """Too-few args makes getMessage() raise; the fallback still scrubs secrets."""
    record = _make_record("bad %s %s with secret=topsecretvalue", ("only-one-arg",))
    assert RedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "topsecretvalue" not in rendered
    assert REDACTION_PLACEHOLDER in rendered


# --------------------------------------------------------------------------- #
# get_logger wiring
# --------------------------------------------------------------------------- #
def test_get_logger_namespaces_under_package_root() -> None:
    """Loggers are reparented under the redacting package-root logger."""
    assert get_logger("foo").name == "trader_mcp.foo"
    assert get_logger("trader_mcp.bar").name == "trader_mcp.bar"
    assert get_logger("trader_mcp").name == "trader_mcp"


def test_package_root_logger_carries_redaction_filter() -> None:
    """Emitting through the configured package logger redacts secrets on stderr."""
    get_logger("wiring_probe")  # triggers one-time configuration
    root = logging.getLogger("trader_mcp")
    assert root.handlers, "package root logger should have a handler after configure()"
    assert any(any(isinstance(f, RedactionFilter) for f in h.filters) for h in root.handlers), (
        "package handler must carry the RedactionFilter"
    )
