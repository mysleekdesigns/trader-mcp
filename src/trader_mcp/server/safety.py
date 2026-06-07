"""Phase 6 guardrails-only safety MCP tools (PRD §5.2 "safety").

Exposes the process-wide :class:`~trader_mcp.safety.SafetyController` as a typed,
structured-output MCP tool surface: configure risk limits, drive the (expiring,
confirmation-gated) live-arming state machine, engage/reset the kill switch, and
read the safety status + the redacted audit trail.

GUARDRAILS-ONLY (the hard boundary for this phase). These tools configure and
observe the safety machinery; they do NOT open the live wall. Concretely:

    * ``ServerSessionMode`` stays ``paper | testnet`` -- there is no ``live``
      session mode, and :func:`trader_mcp.safety.policy.evaluate_order` still
      denies ``live`` unconditionally.
    * ``arm_live_trading`` drives the arm STATE (and audits it) but NO live order
      path consumes that state yet -- that is a documented deferral to live
      certification. The arm is the explicit, expiring opt-in the future live wall
      will require; today it gates nothing.

The risk limits set here ARE enforced now: ``place_order`` runs every intent
through :meth:`SafetyController.preflight`, so a configured ``max_order_notional``
(etc.) denies an over-limit paper/testnet order today, and the kill switch halts
NEW orders for a scope immediately.

Secrets are radioactive: no tool here accepts or echoes a secret, and every
free-text ``detail`` persisted to the audit log is redacted by the log itself.

Typed I/O: every tool takes Pydantic-validated arguments and returns a frozen
Pydantic v2 model (a safety model or a thin wrapper defined here) with
``structured_output=True`` so FastMCP emits an ``outputSchema``.

The MCP SDK stays isolated: registration goes through the ``FastMCP`` instance
re-exported from :mod:`trader_mcp.server._sdk`; this module never ``import mcp``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from trader_mcp.execution import PaperBroker, SessionRegistry
from trader_mcp.safety import (
    ArmTicket,
    AuditEntry,
    RiskLimits,
    SafetyController,
    SafetyStatus,
)
from trader_mcp.server._sdk import FastMCP

if TYPE_CHECKING:
    from collections.abc import Mapping


class _SafetyResult(BaseModel):
    """Base for the thin server-side safety result wrappers (frozen, strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DisarmResult(_SafetyResult):
    """Outcome of a disarm request (object wrapper for a scalar scope)."""

    disarmed: str = Field(
        description="The scope that was disarmed ('*' for all exchanges, else the ccxt id)."
    )
    detail: str = Field(description="Human-readable, secret-free outcome note.")


class KillSwitchResult(_SafetyResult):
    """Outcome of a kill-switch action (engage/reset) plus any cancel-all side effect."""

    scope: str = Field(description="The halt scope ('*' for global, else the ccxt id).")
    engaged: bool = Field(description="True after an engage, False after a reset.")
    orders_canceled: int = Field(
        description=(
            "Number of open PAPER orders best-effort canceled across in-scope sessions on "
            "engage (0 on reset). Testnet cancel-all is deferred."
        )
    )
    detail: str = Field(description="Human-readable, secret-free outcome note.")


class AuditLogResult(_SafetyResult):
    """A page of redacted audit entries plus their count (object wrapper for the list)."""

    entries: list[AuditEntry] = Field(description="Audit entries, newest-last, already redacted.")
    count: int = Field(description="Number of entries returned.")


def register_safety_tools(
    app: FastMCP,
    controller: SafetyController,
    session_registry: SessionRegistry,
    manual_brokers: Mapping[str, PaperBroker] | None = None,
) -> None:
    """Register the Phase 6 guardrails-only safety tools on ``app``.

    The tool callables close over the single process-wide
    :class:`~trader_mcp.safety.SafetyController` (the risk/arm/kill-switch/audit
    aggregator), the in-memory :class:`~trader_mcp.execution.SessionRegistry` (so
    ``kill_switch engage`` can best-effort cancel open orders on in-scope deployed
    sessions), and -- optionally -- the execution lane's MANUAL paper brokers (so a
    halt also cancels open orders placed via ``place_order`` on undeployed
    sessions). This is the only place these tools are registered; ``build_app``
    invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        controller: The shared process-wide safety controller (state + audit).
        session_registry: The shared in-memory execution-session registry; its
            deployed sessions' brokers are scanned for the kill-switch cancel-all.
        manual_brokers: The execution lane's per-session manual paper brokers (the
            same mapping ``register_execution_tools`` records manual ``place_order``
            brokers into). Passed so a halt also cancels open MANUAL paper orders.
            ``None`` (or empty) simply means the cancel-all only covers deployed
            sessions.
    """
    _manual_brokers: Mapping[str, PaperBroker] = (
        manual_brokers if manual_brokers is not None else {}
    )

    @app.tool(
        name="set_risk_limits",
        title="Set risk limits",
        description=(
            "Configure the per-strategy/global risk caps (all optional; null disables a cap): "
            "max_order_notional, max_position_notional, max_open_positions, max_daily_loss, "
            "max_leverage. THESE ARE ENFORCED on every place_order today (paper/testnet) -- an "
            "order breaching a cap is denied with a redacted risk reason -- and will gate live "
            "orders once live trading is certified. NOTE: the notional caps "
            "(max_order_notional / max_position_notional) require a KNOWN price to evaluate -- "
            "a limit order's price, or a session with market data (a live position mark). A "
            "market order with no derivable price is denied (fail-closed) while a notional cap "
            "is set. Returns the stored limits."
        ),
        structured_output=True,
    )
    def set_risk_limits(
        max_order_notional: float | None = None,
        max_position_notional: float | None = None,
        max_open_positions: int | None = None,
        max_daily_loss: float | None = None,
        max_leverage: float | None = None,
    ) -> RiskLimits:
        """Replace the active risk limits and return the stored :class:`RiskLimits`."""
        limits = RiskLimits(
            max_order_notional=max_order_notional,
            max_position_notional=max_position_notional,
            max_open_positions=max_open_positions,
            max_daily_loss=max_daily_loss,
            max_leverage=max_leverage,
        )
        return controller.set_risk_limits(limits)

    @app.tool(
        name="arm_live_trading",
        title="Arm live trading (expiring opt-in; gates nothing yet)",
        description=(
            "Record an explicit, EXPIRING opt-in to live trading for an exchange (or '*' for "
            "all). Requires the EXACT confirmation phrase 'I UNDERSTAND THE RISKS' -- a wrong "
            "phrase surfaces a redacted safety error. The arm expires after ttl_seconds "
            "(default 900s, clamped to at most 3600s) and can be revoked with "
            "disarm_live_trading. IMPORTANT: this drives the arming STATE only -- NO live order "
            "path consumes it yet (live routing is deferred to live certification; the gate "
            "still denies live unconditionally). It is the opt-in the future live wall will "
            "require. Returns the active arm ticket (exchange, armed_at, armed_until)."
        ),
        structured_output=True,
    )
    def arm_live_trading(
        exchange: str,
        confirm: str,
        ttl_seconds: int = 900,
    ) -> ArmTicket:
        """Arm live trading for ``exchange`` (state only) and return the :class:`ArmTicket`.

        Raises a redacted :class:`~trader_mcp.errors.SafetyError` on a wrong
        confirmation phrase or a non-positive TTL (fail closed). Opening the live
        wall on this ticket is the deferred certification step; nothing routes today.
        """
        return controller.arm_live(exchange=exchange, confirm=confirm, ttl_seconds=ttl_seconds)

    @app.tool(
        name="disarm_live_trading",
        title="Disarm live trading",
        description=(
            "Revoke a live-trading arm. Pass an exchange to disarm just that scope, or omit it "
            "(null) to disarm ALL scopes. Idempotent -- disarming a scope that was not armed is "
            "a no-op. Returns the disarmed scope."
        ),
        structured_output=True,
    )
    def disarm_live_trading(exchange: str | None = None) -> DisarmResult:
        """Disarm one scope (or all when ``exchange`` is null); return the disarmed scope."""
        controller.disarm(exchange=exchange)
        scope = "*" if exchange is None else exchange
        return DisarmResult(
            disarmed=scope,
            detail="disarmed all scopes" if exchange is None else f"disarmed {scope}",
        )

    @app.tool(
        name="kill_switch",
        title="Kill switch (engage/reset)",
        description=(
            "Engage or reset the trading halt. action='engage' HALTS new orders for the scope "
            "(an exchange, or '*'/null for a global halt) -- every subsequent place_order for an "
            "in-scope session is DENIED -- and best-effort cancels open PAPER orders across "
            "in-scope sessions (testnet cancel-all is deferred). action='reset' clears the halt "
            "(reset all when scope is null). Cancels DO still work while halted, so a halt can be "
            "cleaned up. Returns the scope, whether it is now engaged, and how many open paper "
            "orders were canceled."
        ),
        structured_output=True,
    )
    def kill_switch(
        action: Literal["engage", "reset"],
        exchange: str | None = None,
        reason: str = "",
    ) -> KillSwitchResult:
        """Engage or reset the kill switch for a scope; on engage, cancel open paper orders."""
        scope = "*" if exchange is None else exchange
        if action == "reset":
            controller.reset_kill_switch(exchange=exchange)
            return KillSwitchResult(
                scope=scope,
                engaged=False,
                orders_canceled=0,
                detail="reset all halts" if exchange is None else f"reset halt for {scope}",
            )

        controller.engage_kill_switch(exchange=exchange, reason=reason)
        canceled = _cancel_open_paper_orders(in_scope=exchange)
        return KillSwitchResult(
            scope=scope,
            engaged=True,
            orders_canceled=canceled,
            detail=("global halt engaged" if exchange is None else f"halt engaged for {scope}")
            + f"; canceled {canceled} open paper order(s)",
        )

    def _cancel_open_paper_orders(*, in_scope: str | None) -> int:
        """Best-effort cancel every open PAPER order on in-scope sessions; return the count.

        Scans the registry's deployed-session brokers AND the execution lane's manual
        paper brokers. A ``None`` scope is global (every session); otherwise only
        sessions on ``in_scope`` exchange are touched. Testnet sessions are skipped
        (testnet cancel-all is a deferred follow-up). Reads broker state only.
        """
        canceled = 0
        for info in session_registry.list():
            if in_scope is not None and info.exchange != in_scope:
                continue
            if info.mode != "paper":
                continue  # testnet cancel-all deferred
            broker = session_registry.broker_for(info.session_id) or _manual_brokers.get(
                info.session_id
            )
            if broker is None:
                continue
            for order in list(broker.open_orders()):
                if broker.cancel(order.order_id):
                    canceled += 1
        return canceled

    @app.tool(
        name="get_safety_status",
        title="Get safety status",
        description=(
            "Return a read-only snapshot of the safety controller: the process-wide dry-run "
            "default, the active risk limits, the currently-active arm tickets, the engaged "
            "halt scopes, and the number of audit entries recorded. No secrets are returned."
        ),
        structured_output=True,
    )
    def get_safety_status() -> SafetyStatus:
        """Return the controller's :class:`SafetyStatus` snapshot."""
        return controller.status()

    @app.tool(
        name="get_audit_log",
        title="Get safety audit log",
        description=(
            "Return the append-only, REDACTED safety audit trail (order intents/results, "
            "denials, kill-switch/arm/disarm/risk-limit changes), newest-last. Optionally cap "
            "the page with limit and filter by event type (e.g. 'order_intent', 'order_result', "
            "'denied', 'kill_switch', 'arm', 'disarm', 'set_risk_limits'). Free-text detail is "
            "redacted on record, so no entry carries secret material. Returns the entries plus a "
            "count."
        ),
        structured_output=True,
    )
    def get_audit_log(limit: int | None = None, event: str | None = None) -> AuditLogResult:
        """Return a page of redacted :class:`AuditEntry` records plus their count."""
        entries = controller.audit.entries(limit=limit, event=event)
        return AuditLogResult(entries=entries, count=len(entries))
