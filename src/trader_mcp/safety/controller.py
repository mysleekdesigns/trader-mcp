"""The process-wide safety aggregator the MCP server holds one of.

INVARIANT (safety): every order-placing path runs through :meth:`SafetyController.
preflight`, which fails closed in priority order -- kill switch first, then risk
limits, then the gate -- and records an audit entry for every intent and every
state change. The arming state machine is owned and exercised here but is **NOT
consulted by the gate's ``live`` wall**: :func:`trader_mcp.safety.policy.
evaluate_order` still denies ``live`` unconditionally regardless of arm state.
Wiring arming into the wall is the deferred Phase-6 certification step.

Instantiable: one per ``build_app`` on the server, a fresh one per test. No global
singleton. ``now`` is stamped here (UTC wall clock) so the lower modules stay pure.

Self-contained: imports only sibling safety modules + config/errors/logging.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trader_mcp.config import KeyScope
from trader_mcp.safety.arming import (
    DEFAULT_ARM_TTL_SECONDS,
    ArmingRegistry,
    ArmTicket,
)
from trader_mcp.safety.audit import AuditEntry, AuditLog
from trader_mcp.safety.kill_switch import KillSwitchState
from trader_mcp.safety.policy import GateDecision, SessionMode, evaluate_order, global_dry_run
from trader_mcp.safety.risk import (
    OrderRiskContext,
    RiskCheck,
    RiskLimits,
    check_order_risk,
)
from trader_mcp.safety.scoping import MarketType


class SafetyStatus(BaseModel):
    """A read-only snapshot of the controller's state for a status tool. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    risk_limits: RiskLimits
    armed: list[ArmTicket] = Field(default_factory=list)
    halted_scopes: list[str] = Field(default_factory=list)
    audit_entries: int


class SafetyController:
    """Aggregates risk limits + arming + kill switch + audit behind one object."""

    def __init__(self, *, risk_limits: RiskLimits | None = None) -> None:
        self._limits: RiskLimits = risk_limits if risk_limits is not None else RiskLimits()
        self._arming = ArmingRegistry()
        self._kill_switch = KillSwitchState()
        self._audit = AuditLog()

    # ---- internal helpers ----------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _record(self, entry: AuditEntry) -> AuditEntry:
        return self._audit.record(entry)

    # ---- risk-limit config ---------------------------------------------------

    def set_risk_limits(self, limits: RiskLimits) -> RiskLimits:
        """Replace the active risk limits; record an audit entry. Returns the new limits."""
        self._limits = limits
        self._record(
            AuditEntry(
                timestamp=self._now(),
                event="set_risk_limits",
                detail=f"risk limits updated: {limits.model_dump(exclude_none=True)}",
            )
        )
        return self._limits

    def get_risk_limits(self) -> RiskLimits:
        """Return the active risk limits."""
        return self._limits

    # ---- arming (built + tested; NOT wired to the live wall) -----------------

    def arm_live(
        self,
        *,
        exchange: str,
        confirm: str,
        ttl_seconds: int = DEFAULT_ARM_TTL_SECONDS,
    ) -> ArmTicket:
        """Arm live trading for ``exchange`` (stamps now=UTC), recording an audit entry.

        NOTE: arming does NOT open the live wall -- :func:`evaluate_order` still
        denies ``live``. This drives the state machine only.
        """
        ticket = self._arming.arm(
            exchange=exchange, confirm=confirm, now=self._now(), ttl_seconds=ttl_seconds
        )
        self._record(
            AuditEntry(
                timestamp=self._now(),
                event="arm",
                exchange=ticket.exchange,
                detail=f"armed until {ticket.armed_until.isoformat()}",
            )
        )
        return ticket

    def disarm(self, *, exchange: str | None = None) -> None:
        """Disarm one exchange, or ALL when ``exchange`` is ``None``; records audit."""
        self._arming.disarm(exchange)
        self._record(
            AuditEntry(
                timestamp=self._now(),
                event="disarm",
                exchange=exchange,
                detail="disarmed all" if exchange is None else f"disarmed {exchange}",
            )
        )

    def is_armed(self, exchange: str) -> bool:
        """True iff an active arm ticket covers ``exchange`` right now."""
        return self._arming.is_armed(exchange, self._now())

    # ---- kill switch ---------------------------------------------------------

    def engage_kill_switch(self, *, exchange: str | None = None, reason: str = "") -> None:
        """Engage the halt (global when ``exchange`` is ``None``); records audit."""
        self._kill_switch.engage(exchange=exchange, reason=reason)
        self._record(
            AuditEntry(
                timestamp=self._now(),
                event="kill_switch",
                exchange=exchange,
                outcome="engaged",
                detail=reason or ("global halt" if exchange is None else f"halt {exchange}"),
            )
        )

    def reset_kill_switch(self, *, exchange: str | None = None) -> None:
        """Clear the halt (global + all when ``exchange`` is ``None``); records audit."""
        self._kill_switch.reset(exchange=exchange)
        self._record(
            AuditEntry(
                timestamp=self._now(),
                event="kill_switch",
                exchange=exchange,
                outcome="reset",
                detail="reset all" if exchange is None else f"reset {exchange}",
            )
        )

    def is_halted(self, exchange: str) -> bool:
        """True iff the global halt or this exchange's halt is engaged."""
        return self._kill_switch.is_halted(exchange)

    # ---- the order chokepoint ------------------------------------------------

    def preflight(
        self,
        *,
        mode: SessionMode | str,
        exchange: str,
        symbol: str,
        market_type: MarketType,
        key_scope: KeyScope,
        side: str,
        amount: float,
        notional: float,
        risk_ctx: OrderRiskContext,
        dry_run: bool | None = None,
    ) -> GateDecision:
        """Run every order intent through the full safety pipeline, fail-closed.

        Order of checks (first failure wins, all fail closed):
          1. Kill switch halted for ``exchange`` -> deny + audit ``denied``.
          2. Risk limits breached -> deny + audit ``denied`` (violations joined).
          3. :func:`evaluate_order` (``live`` still denies inside it -- unchanged).
          4. Record an ``order_intent`` audit entry carrying the decision; return it.

        Returns:
            The :class:`GateDecision` (``simulate`` / ``route`` / ``deny``). The
            controller never routes a real order itself; the caller honors the verdict.
        """
        resolved_mode = mode if isinstance(mode, SessionMode) else str(mode)

        # 1) Kill switch -- hardest stop, checked first.
        if self._kill_switch.is_halted(exchange):
            reason = "kill switch engaged: trading halted for this scope"
            ks_reason = self._kill_switch.reason_for(exchange)
            if ks_reason:
                reason = f"{reason} ({ks_reason})"
            decision = GateDecision(action="deny", mode=_as_mode(resolved_mode), reason=reason)
            self._record_intent(
                event="denied",
                mode=str(resolved_mode),
                exchange=exchange,
                symbol=symbol,
                side=side,
                amount=amount,
                notional=notional,
                decision=decision.action,
                detail=reason,
            )
            return decision

        # 2) Risk limits -- enforced server-side, not advisory.
        risk: RiskCheck = check_order_risk(risk_ctx, self._limits)
        if not risk.passed:
            reason = "risk limit breached: " + "; ".join(risk.violations)
            decision = GateDecision(action="deny", mode=_as_mode(resolved_mode), reason=reason)
            self._record_intent(
                event="denied",
                mode=str(resolved_mode),
                exchange=exchange,
                symbol=symbol,
                side=side,
                amount=amount,
                notional=notional,
                decision=decision.action,
                detail=reason,
            )
            return decision

        # 3) The gate. live still denies inside evaluate_order -- unchanged.
        decision = evaluate_order(
            mode=resolved_mode,
            exchange=exchange,
            market_type=market_type,
            key_scope=key_scope,
            amount=amount,
            notional=notional,
            dry_run=dry_run,
        )

        # 4) Audit the intent carrying the decision.
        self._record_intent(
            event="order_intent",
            mode=str(decision.mode),
            exchange=exchange,
            symbol=symbol,
            side=side,
            amount=amount,
            notional=notional,
            decision=decision.action,
            detail=decision.reason,
        )
        return decision

    def _record_intent(
        self,
        *,
        event: str,
        mode: str,
        exchange: str,
        symbol: str,
        side: str,
        amount: float,
        notional: float,
        decision: str,
        detail: str,
    ) -> None:
        # event is constrained to the audit Literal at the two call sites above.
        self._record(
            AuditEntry(
                timestamp=self._now(),
                event=event,  # type: ignore[arg-type]
                mode=mode,
                exchange=exchange,
                symbol=symbol,
                side=side,
                amount=amount,
                notional=notional,
                decision=decision,
                detail=detail,
            )
        )

    # ---- introspection -------------------------------------------------------

    @property
    def audit(self) -> AuditLog:
        """The append-only audit log."""
        return self._audit

    def status(self) -> SafetyStatus:
        """A snapshot of dry-run, limits, active arms, halts, and audit size."""
        now = self._now()
        return SafetyStatus(
            dry_run=global_dry_run(),
            risk_limits=self._limits,
            armed=self._arming.active_tickets(now),
            halted_scopes=self._kill_switch.halted_scopes(),
            audit_entries=len(self._audit),
        )


def _as_mode(mode: SessionMode | str) -> SessionMode:
    """Best-effort coerce a mode for a GateDecision; unknown -> LIVE (most restricted)."""
    if isinstance(mode, SessionMode):
        return mode
    try:
        return SessionMode(str(mode).strip().lower())
    except ValueError:
        return SessionMode.LIVE
