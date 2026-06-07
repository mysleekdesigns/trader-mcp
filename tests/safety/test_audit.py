"""Audit-log tests.

Proves the safety-critical invariant: the free-text ``detail`` is redacted on
record so a secret can never be persisted in the clear. Also: ring-buffer bound,
entries(limit=N) returns the last N, and the event filter works.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trader_mcp.logging_config import REDACTION_PLACEHOLDER
from trader_mcp.safety.audit import AuditEntry, AuditLog

TS = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def _entry(event: str = "order_intent", detail: str | None = None) -> AuditEntry:
    return AuditEntry(timestamp=TS, event=event, detail=detail)  # type: ignore[arg-type]


def test_detail_is_redacted_on_record() -> None:
    log = AuditLog()
    stored = log.record(
        _entry(detail="connect failed apiKey=SECRET123ABCDEF0987654321XYZLONGTOKEN")
    )
    assert "SECRET123ABCDEF0987654321XYZLONGTOKEN" not in (stored.detail or "")
    assert REDACTION_PLACEHOLDER in (stored.detail or "")
    # And the persisted copy is the redacted one.
    assert log.entries()[-1].detail == stored.detail


def test_none_detail_passes_through() -> None:
    log = AuditLog()
    stored = log.record(_entry(detail=None))
    assert stored.detail is None


def test_ring_buffer_bound() -> None:
    log = AuditLog(capacity=3)
    for i in range(10):
        log.record(_entry(detail=f"entry {i}"))
    assert len(log) == 3
    # Newest survive; oldest dropped.
    details = [e.detail for e in log.entries()]
    assert details == ["entry 7", "entry 8", "entry 9"]


def test_entries_limit_returns_last_n() -> None:
    log = AuditLog()
    for i in range(5):
        log.record(_entry(detail=f"e{i}"))
    last_two = log.entries(limit=2)
    assert [e.detail for e in last_two] == ["e3", "e4"]


def test_entries_limit_zero_is_empty() -> None:
    log = AuditLog()
    log.record(_entry(detail="x"))
    assert log.entries(limit=0) == []


def test_event_filter() -> None:
    log = AuditLog()
    log.record(_entry(event="order_intent", detail="a"))
    log.record(_entry(event="denied", detail="b"))
    log.record(_entry(event="order_intent", detail="c"))
    denied = log.entries(event="denied")
    assert [e.detail for e in denied] == ["b"]


def test_event_filter_with_limit() -> None:
    log = AuditLog()
    for i in range(4):
        log.record(_entry(event="arm", detail=f"arm{i}"))
        log.record(_entry(event="disarm", detail=f"disarm{i}"))
    result = log.entries(event="arm", limit=2)
    assert [e.detail for e in result] == ["arm2", "arm3"]
