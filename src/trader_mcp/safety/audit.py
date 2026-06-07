"""Append-only, redacted audit trail of order intents/results and safety events.

INVARIANT (safety): the audit trail records every order intent/result and every
safety state change, and **its free-text ``detail`` field is always redacted** on
record so a secret embedded in an upstream error message can never be persisted in
the clear. The log is an in-memory, bounded ring buffer -- the newest entries
survive; the oldest are dropped past the cap.

Self-contained: stdlib + pydantic + :mod:`trader_mcp.logging_config` (``redact``).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader_mcp.logging_config import redact

#: Default ring-buffer capacity. Oldest entries are evicted past this bound.
DEFAULT_AUDIT_CAPACITY = 10_000

#: The event categories the audit trail recognizes.
AuditEvent = Literal[
    "order_intent",
    "order_result",
    "denied",
    "kill_switch",
    "arm",
    "disarm",
    "set_risk_limits",
]


class AuditEntry(BaseModel):
    """One audit record. Immutable.

    The free-text :attr:`detail` is redacted by :meth:`AuditLog.record`; construct
    entries freely and let the log scrub them on the way in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    event: AuditEvent
    mode: str | None = None
    exchange: str | None = None
    symbol: str | None = None
    side: str | None = None
    amount: float | None = None
    notional: float | None = None
    decision: str | None = None  # simulate / route / deny
    outcome: str | None = None  # filled / open / rejected / canceled / error
    detail: str | None = None  # free text -- MUST be redacted on record()


class AuditLog:
    """Append-only in-memory ring buffer of :class:`AuditEntry`. Instantiable.

    Bounded (default :data:`DEFAULT_AUDIT_CAPACITY`); newest-last ordering.
    """

    def __init__(self, *, capacity: int = DEFAULT_AUDIT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("audit capacity must be positive")
        self._entries: deque[AuditEntry] = deque(maxlen=capacity)

    def record(self, entry: AuditEntry) -> AuditEntry:
        """Append ``entry`` with its ``detail`` redacted; return the stored entry.

        Redaction is applied to the free-text ``detail`` only -- the structured
        fields are enum/typed values that never carry secret material. The stored
        (redacted) entry is returned so callers log exactly what was persisted.
        """
        if entry.detail is not None:
            entry = entry.model_copy(update={"detail": redact(entry.detail)})
        self._entries.append(entry)
        return entry

    def entries(self, *, limit: int | None = None, event: str | None = None) -> list[AuditEntry]:
        """Return recorded entries, newest-last.

        Args:
            limit: If set, return only the most recent ``limit`` (after filtering).
            event: If set, return only entries of this event type.
        """
        items = list(self._entries)
        if event is not None:
            items = [e for e in items if e.event == event]
        if limit is not None:
            items = items[-limit:] if limit > 0 else []
        return items

    def __len__(self) -> int:
        return len(self._entries)
