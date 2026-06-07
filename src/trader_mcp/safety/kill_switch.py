"""The kill switch: a reliable global + per-exchange halt (Phase 6).

INVARIANT (safety): ``kill_switch`` must reliably halt live activity. This models
the halt state -- the controller checks :meth:`KillSwitchState.is_halted` at the
top of every order preflight and denies when engaged, so a halt fails closed. A
global engage stops every exchange; a per-exchange engage stops only that one.

Cancel-all / flatten side effects are wired at the execution layer (not here, to
keep the safety package self-contained); this object is the authoritative halt
*state* every order path consults. Instantiable, in-memory, no global singleton.

Self-contained: stdlib only.
"""

from __future__ import annotations

from trader_mcp.safety.arming import GLOBAL_SCOPE


def _normalize(exchange: str) -> str:
    normalized = exchange.strip().lower()
    return normalized


class KillSwitchState:
    """Authoritative halt state. Instantiable, in-memory.

    Tracks a global halt flag plus a set of per-exchange halts, each with an
    optional secret-free reason for status display.
    """

    def __init__(self) -> None:
        self._global_engaged: bool = False
        self._global_reason: str = ""
        # Normalized exchange id -> reason string.
        self._exchange_reasons: dict[str, str] = {}

    def engage(self, *, exchange: str | None = None, reason: str = "") -> None:
        """Engage the halt. ``None`` engages the GLOBAL halt; else just ``exchange``.

        Re-engaging updates the stored reason.
        """
        if exchange is None:
            self._global_engaged = True
            self._global_reason = reason
            return
        self._exchange_reasons[_normalize(exchange)] = reason

    def reset(self, *, exchange: str | None = None) -> None:
        """Clear a halt. ``None`` clears the GLOBAL halt AND every per-exchange halt.

        Resetting a scope that was not engaged is a no-op (idempotent).
        """
        if exchange is None:
            self._global_engaged = False
            self._global_reason = ""
            self._exchange_reasons.clear()
            return
        self._exchange_reasons.pop(_normalize(exchange), None)

    def is_halted(self, exchange: str) -> bool:
        """True iff the global halt is engaged OR this exchange is halted."""
        if self._global_engaged:
            return True
        return _normalize(exchange) in self._exchange_reasons

    def halted_scopes(self) -> list[str]:
        """The engaged scopes for status: :data:`GLOBAL_SCOPE` and/or exchange ids."""
        scopes: list[str] = []
        if self._global_engaged:
            scopes.append(GLOBAL_SCOPE)
        scopes.extend(sorted(self._exchange_reasons))
        return scopes

    def reason_for(self, exchange: str) -> str | None:
        """The reason halting ``exchange``, or ``None`` if it is not halted.

        A global halt takes precedence and surfaces its global reason.
        """
        if self._global_engaged:
            return self._global_reason
        return self._exchange_reasons.get(_normalize(exchange))
