"""Safety layer (owned by ``risk-safety-engineer``).

INVARIANT: safe-by-default execution. Strategy specs are data, never code.
``place_order``/``deploy_strategy`` are DRY-RUN unless explicitly armed. In
Phase 5 the only execution modes are ``paper`` (pure simulation -- always safe)
and ``testnet`` (real API calls against an exchange sandbox / demo, fake money).
**Real-money LIVE trading is Phase 6 and is BLOCKED here**: the gate denies
``mode="live"`` unconditionally. Keys are scoped read-only vs trade-enabled and a
read-only credential is structurally unable to trade. Secrets come from
env/.env/keyring and are never logged, printed, or returned.

The single chokepoint is :func:`evaluate_order`, which returns a frozen
:class:`GateDecision` (``simulate`` / ``route`` / ``deny``). Every order-placing
path (execution runtime, MCP server) MUST consult it and honor the verdict before
constructing any exchange order. A process-wide dry-run default (on unless
``TRADER_MCP_DRY_RUN`` disables it) downgrades any ``route`` to ``simulate``, so
the safe default holds without per-call configuration.

The secret-handling and key-*scoping* primitives that back this invariant live in
:mod:`trader_mcp.config` (:class:`~trader_mcp.config.KeyScope`,
:class:`~trader_mcp.config.ExchangeCredentials`) and the redaction filter in
:mod:`trader_mcp.logging_config`. The arming state machine, risk engine, and
``kill_switch`` land in Phase 6 and must not be wired before then.
"""

from __future__ import annotations

from trader_mcp.safety.arming import (
    DEFAULT_ARM_TTL_SECONDS,
    GLOBAL_SCOPE,
    MAX_ARM_TTL_SECONDS,
    REQUIRED_CONFIRMATION,
    ArmingRegistry,
    ArmTicket,
)
from trader_mcp.safety.audit import AuditEntry, AuditLog
from trader_mcp.safety.controller import SafetyController, SafetyStatus
from trader_mcp.safety.idempotency import (
    COID_PREFIX,
    IdempotencyRegistry,
    OrderSide,
    make_client_order_id,
)
from trader_mcp.safety.kill_switch import KillSwitchState
from trader_mcp.safety.policy import (
    GateDecision,
    SessionMode,
    evaluate_order,
    global_dry_run,
)
from trader_mcp.safety.risk import (
    OrderRiskContext,
    RiskCheck,
    RiskLimits,
    check_order_risk,
)
from trader_mcp.safety.scoping import (
    JURISDICTION_DISCLAIMER,
    US_ELIGIBLE_EXCHANGES,
    US_PERP_EXCHANGES,
    MarketType,
    check_jurisdiction,
    require_trade_scope,
)

#: Marker affirming the safe-by-default invariant for this package. Anything that
#: places, arms, or routes orders must default to dry-run until Phase 6.
SAFE_BY_DEFAULT = True

__all__ = [
    "COID_PREFIX",
    "DEFAULT_ARM_TTL_SECONDS",
    "GLOBAL_SCOPE",
    "JURISDICTION_DISCLAIMER",
    "MAX_ARM_TTL_SECONDS",
    "REQUIRED_CONFIRMATION",
    "SAFE_BY_DEFAULT",
    "US_ELIGIBLE_EXCHANGES",
    "US_PERP_EXCHANGES",
    "ArmTicket",
    "ArmingRegistry",
    "AuditEntry",
    "AuditLog",
    "GateDecision",
    "IdempotencyRegistry",
    "KillSwitchState",
    "MarketType",
    "OrderRiskContext",
    "OrderSide",
    "RiskCheck",
    "RiskLimits",
    "SafetyController",
    "SafetyStatus",
    "SessionMode",
    "check_jurisdiction",
    "check_order_risk",
    "evaluate_order",
    "global_dry_run",
    "make_client_order_id",
    "require_trade_scope",
]
