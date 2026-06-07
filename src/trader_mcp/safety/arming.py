"""The live-arming state machine (BUILT + UNIT-TESTED, NOT wired to the wall).

INVARIANT (safety / Phase 6 gating): arming is explicit, confirmation-gated,
expiring, and revocable. This module models that state machine so the controller
and (eventually) the ``arm_live_trading`` tool can drive it. **It is deliberately
NOT consulted by** :func:`trader_mcp.safety.policy.evaluate_order` -- the ``live``
branch there still denies unconditionally. Opening the live wall is the deferred
certification step (PRD §6); arming alone never routes a real order.

Design: ``now`` is passed in explicitly so the core logic is pure and testable;
the convenience that stamps wall-clock time lives at the controller layer, not
here. The registry is an in-memory, instantiable object -- no global singleton --
so each server build and each test gets a fresh state machine.

Self-contained: imports only stdlib + pydantic + :mod:`trader_mcp.errors`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from trader_mcp.errors import SafetyError

#: The exact confirmation phrase a caller must supply to arm live trading. A
#: mismatch fails closed with a :class:`SafetyError`.
REQUIRED_CONFIRMATION: Final[str] = "I UNDERSTAND THE RISKS"

#: Sentinel exchange value meaning "all exchanges" (a global arm).
GLOBAL_SCOPE: Final[str] = "*"

#: Default arm time-to-live: 15 minutes. Arming always expires.
DEFAULT_ARM_TTL_SECONDS: Final[int] = 900

#: Hard ceiling on an arm TTL. Requests above this are clamped down.
MAX_ARM_TTL_SECONDS: Final[int] = 3600


def _normalize_exchange(exchange: str) -> str:
    """Lower/strip an exchange id; :data:`GLOBAL_SCOPE` passes through unchanged."""
    normalized = exchange.strip().lower()
    if not normalized:
        raise SafetyError("exchange must be a non-empty id or the global scope '*'")
    return normalized


class ArmTicket(BaseModel):
    """An active (or expired) arm grant for one exchange (or the global scope).

    Immutable. Activity is a pure function of a supplied ``now`` -- the ticket
    holds no live clock.

    Attributes:
        exchange: Normalized ccxt id, or :data:`GLOBAL_SCOPE` for all exchanges.
        armed_at: When the arm was granted (tz-aware UTC).
        armed_until: When the arm expires (tz-aware UTC).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str
    armed_at: datetime
    armed_until: datetime

    def is_active(self, now: datetime) -> bool:
        """True iff ``now`` is within ``[armed_at, armed_until)``."""
        return self.armed_at <= now < self.armed_until

    def seconds_remaining(self, now: datetime) -> float:
        """Seconds until expiry from ``now`` (0.0 once expired; never negative)."""
        remaining = (self.armed_until - now).total_seconds()
        return remaining if remaining > 0 else 0.0


class ArmingRegistry:
    """In-memory, confirmation-gated, expiring arm state. Instantiable.

    There is no global singleton -- the controller owns one, and tests construct
    their own. ``now`` is always supplied by the caller so behavior is deterministic.
    """

    def __init__(self) -> None:
        # Keyed by normalized exchange id (or GLOBAL_SCOPE). At most one ticket per
        # scope; arming the same scope again replaces the prior ticket.
        self._tickets: dict[str, ArmTicket] = {}

    def arm(
        self,
        *,
        exchange: str,
        confirm: str,
        now: datetime,
        ttl_seconds: int = DEFAULT_ARM_TTL_SECONDS,
    ) -> ArmTicket:
        """Grant an expiring arm for ``exchange`` (or :data:`GLOBAL_SCOPE`).

        Fails closed if the confirmation phrase is wrong or the TTL is non-positive.
        A TTL above :data:`MAX_ARM_TTL_SECONDS` is clamped down to the cap.

        Args:
            exchange: ccxt id (normalized) or :data:`GLOBAL_SCOPE`.
            confirm: Must equal :data:`REQUIRED_CONFIRMATION` exactly.
            now: Current tz-aware UTC time (supplied by the caller).
            ttl_seconds: Desired lifetime; clamped to ``(0, MAX_ARM_TTL_SECONDS]``.

        Returns:
            The active :class:`ArmTicket` granted.

        Raises:
            SafetyError: on a wrong confirmation phrase or a non-positive TTL.
        """
        if confirm != REQUIRED_CONFIRMATION:
            raise SafetyError(
                "arming requires the exact confirmation phrase; refusing (fail closed)"
            )
        if ttl_seconds <= 0:
            raise SafetyError("arm ttl must be a positive number of seconds")
        ttl = min(ttl_seconds, MAX_ARM_TTL_SECONDS)

        normalized = _normalize_exchange(exchange)
        ticket = ArmTicket(
            exchange=normalized,
            armed_at=now,
            armed_until=now + timedelta(seconds=ttl),
        )
        self._tickets[normalized] = ticket
        return ticket

    def disarm(self, exchange: str | None = None) -> None:
        """Revoke an arm. ``None`` disarms ALL scopes; otherwise just that one.

        Disarming an exchange that was not armed is a no-op (idempotent).
        """
        if exchange is None:
            self._tickets.clear()
            return
        self._tickets.pop(_normalize_exchange(exchange), None)

    def is_armed(self, exchange: str, now: datetime) -> bool:
        """True iff an ACTIVE ticket covers ``exchange`` at ``now``.

        A global-scope ticket (:data:`GLOBAL_SCOPE`) covers every exchange.
        Expired tickets are pruned as a side effect.
        """
        self._prune(now)
        normalized = _normalize_exchange(exchange)
        global_ticket = self._tickets.get(GLOBAL_SCOPE)
        if global_ticket is not None and global_ticket.is_active(now):
            return True
        ticket = self._tickets.get(normalized)
        return ticket is not None and ticket.is_active(now)

    def active_tickets(self, now: datetime) -> list[ArmTicket]:
        """All currently-active tickets, pruning any that have expired."""
        self._prune(now)
        return [t for t in self._tickets.values() if t.is_active(now)]

    def _prune(self, now: datetime) -> None:
        expired = [scope for scope, t in self._tickets.items() if not t.is_active(now)]
        for scope in expired:
            del self._tickets[scope]
