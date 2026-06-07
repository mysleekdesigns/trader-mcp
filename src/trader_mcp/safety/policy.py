"""The safe-by-default execution gate.

INVARIANT (safety): ``place_order`` / ``deploy_strategy`` are DRY-RUN unless
explicitly armed. Every order-placing path calls :func:`evaluate_order` first and
honors the returned :class:`GateDecision` -- the execution runtime and MCP server
MUST NOT construct a real exchange order without a ``route`` decision from here.

Phase 5 modes:
  * ``paper``   -- pure in-process simulation. Always ``simulate``; needs no key.
  * ``testnet`` -- real API calls against an exchange sandbox / demo (fake money).
    ``route`` only when the key is trade-enabled, the venue/market is US-eligible,
    and the global dry-run setting is off. Read-only keys still cannot place orders.
  * ``live``    -- real-money trading. The hard Phase-6 wall: ALWAYS ``deny`` here.

Global dry-run: a process-wide ``dry_run`` default (``True`` unless explicitly
disabled via settings/env) downgrades any ``route`` to ``simulate`` so the safe
default holds without per-call configuration.

Self-contained: depends only on :mod:`trader_mcp.config`, :mod:`trader_mcp.errors`,
and the sibling :mod:`trader_mcp.safety.scoping` -- never on execution/exchanges/
server (written in parallel). The order to gate is modeled with plain typed fields,
not by importing another package's order model.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trader_mcp.config import KeyScope
from trader_mcp.errors import SafetyError
from trader_mcp.safety.scoping import MarketType, check_jurisdiction, require_trade_scope


class SessionMode(StrEnum):
    """Execution mode of a trading session.

    Only :attr:`PAPER` and :attr:`TESTNET` are wired in Phase 5. :attr:`LIVE` is
    declared so the gate can explicitly recognize and DENY real-money trading
    until Phase 6 -- it is never a routable mode here.
    """

    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


#: Env var that disables the safe-by-default global dry-run. The default is ON
#: (dry-run), so the absence/empty/any-non-disable value keeps routing simulated.
_DRY_RUN_ENV: str = "TRADER_MCP_DRY_RUN"

#: Truthy spellings that turn the global dry-run OFF (i.e. allow real routing).
_DRY_RUN_OFF: frozenset[str] = frozenset({"0", "false", "no", "off"})


def global_dry_run() -> bool:
    """Return the process-wide dry-run default.

    Safe by default: ``True`` unless ``TRADER_MCP_DRY_RUN`` is explicitly set to a
    disabling value (``0``/``false``/``no``/``off``). When ``True``, even an
    otherwise-routable ``testnet`` decision is downgraded to ``simulate``.
    """
    raw = os.environ.get(_DRY_RUN_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _DRY_RUN_OFF


class GateDecision(BaseModel):
    """The gate's verdict for a single order intent. Immutable.

    Attributes:
        action: ``simulate`` (run through the paper broker, no real order),
            ``route`` (send to the real exchange -- testnet only in Phase 5), or
            ``deny`` (refuse; the tool surfaces :attr:`reason`).
        mode: The :class:`SessionMode` the decision was made for.
        reason: Human-readable, secret-free explanation of the verdict.
        disclaimer: Optional jurisdiction/eligibility advisory for routed orders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["simulate", "route", "deny"]
    mode: SessionMode
    reason: str
    disclaimer: str | None = Field(default=None)

    @property
    def allowed(self) -> bool:
        """True when the order may proceed (simulated or routed), False if denied."""
        return self.action != "deny"

    def raise_if_denied(self) -> GateDecision:
        """Return ``self`` if allowed; raise :class:`SafetyError` if denied.

        Convenience for callers that prefer to fail closed on a ``deny`` rather
        than branch on :attr:`action`.
        """
        if self.action == "deny":
            raise SafetyError(self.reason, details={"mode": str(self.mode)})
        return self


def evaluate_order(
    *,
    mode: SessionMode | str,
    exchange: str,
    market_type: MarketType,
    key_scope: KeyScope,
    amount: float,
    notional: float,
    armed: bool = False,
    dry_run: bool | None = None,
) -> GateDecision:
    """Decide whether an order is simulated, routed to a real venue, or denied.

    This is the single chokepoint every order-placing path must consult before
    constructing an exchange order. It never places an order itself.

    Decision matrix (mode x scope x jurisdiction -> action):
      * ``paper``                              -> ``simulate`` (always; no scope/venue needed)
      * ``testnet`` + read-only key            -> ``deny`` (read-only cannot trade)
      * ``testnet`` + ineligible venue/market  -> ``deny`` (jurisdiction)
      * ``testnet`` + trade key + US-eligible  -> ``route`` (or ``simulate`` if dry-run)
      * ``live``                               -> ``deny`` (always; Phase-6 wall)
      * unknown / unrecognized mode            -> ``deny`` (fail closed)

    Args:
        mode: Session mode (:class:`SessionMode` or its string value).
        exchange: CCXT exchange id.
        market_type: ``"spot"`` or ``"swap"``.
        key_scope: Declared capability of the credential in use.
        amount: Order size in base units (validated >= 0; informational here).
        notional: Order notional in quote units (validated >= 0; informational).
        armed: Per-strategy arming flag. Reserved for Phase 6 live arming; it
            does NOT enable routing here -- ``live`` is denied regardless of it.
        dry_run: Override the global dry-run default. ``None`` (default) uses
            :func:`global_dry_run`; ``True`` forces simulation; ``False`` permits
            routing for an otherwise-routable ``testnet`` decision.

    Returns:
        A frozen :class:`GateDecision`. The function does not raise for a denied
        order -- it returns ``action="deny"`` with a reason (call
        :meth:`GateDecision.raise_if_denied` to fail closed).
    """
    if amount < 0 or notional < 0:
        return GateDecision(
            action="deny",
            mode=_coerce_mode(mode) or SessionMode.LIVE,
            reason="order amount and notional must be non-negative",
        )

    resolved_mode = _coerce_mode(mode)
    if resolved_mode is None:
        return GateDecision(
            action="deny",
            mode=SessionMode.LIVE,  # treat unknown as the most-restricted bucket
            reason=f"unknown session mode {mode!r}; refusing (fail closed)",
        )

    effective_dry_run = global_dry_run() if dry_run is None else dry_run

    if resolved_mode is SessionMode.PAPER:
        # Pure simulation -- always safe, no key scope or venue eligibility needed.
        return GateDecision(
            action="simulate",
            mode=resolved_mode,
            reason="paper mode: simulated through the in-process broker (no real order)",
        )

    if resolved_mode is SessionMode.LIVE:
        # The hard Phase-6 wall. Armed flag and scope are irrelevant here.
        return GateDecision(
            action="deny",
            mode=resolved_mode,
            reason="real-money live trading is gated to Phase 6; not enabled",
        )

    if resolved_mode is SessionMode.TESTNET:
        # Read-only keys can never place orders, even against a sandbox.
        try:
            require_trade_scope(key_scope)
        except SafetyError as exc:
            return GateDecision(action="deny", mode=resolved_mode, reason=exc.message)
        # Venue/market must be US-eligible (and perps CFTC-regulated).
        try:
            disclaimer = check_jurisdiction(exchange, market_type)
        except SafetyError as exc:
            return GateDecision(action="deny", mode=resolved_mode, reason=exc.message)
        # Trade-enabled + eligible. Honor the global dry-run downgrade.
        if effective_dry_run:
            return GateDecision(
                action="simulate",
                mode=resolved_mode,
                reason="testnet routable, but global dry-run is on: downgraded to simulate",
                disclaimer=disclaimer,
            )
        return GateDecision(
            action="route",
            mode=resolved_mode,
            reason="testnet: routing to exchange sandbox (fake money) with trade-enabled key",
            disclaimer=disclaimer,
        )

    # Defensive: any mode that slips through the branches above is denied.
    return GateDecision(  # pragma: no cover - unreachable given the StrEnum
        action="deny",
        mode=resolved_mode,
        reason=f"unhandled session mode {resolved_mode!r}; refusing (fail closed)",
    )


def _coerce_mode(mode: SessionMode | str) -> SessionMode | None:
    """Coerce a mode value to :class:`SessionMode`, or ``None`` if unrecognized."""
    if isinstance(mode, SessionMode):
        return mode
    try:
        return SessionMode(str(mode).strip().lower())
    except ValueError:
        return None
