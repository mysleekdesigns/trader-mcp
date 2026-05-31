"""Safety layer (owned by ``risk-safety-engineer``).

INVARIANT: safe-by-default execution. Strategy specs are data, never code.
``place_order``/``deploy_strategy`` are dry-run unless explicitly armed; real-money
live trading is gated behind ``arm_live_trading`` + per-strategy risk limits and
does not land until Phase 6. Keys are scoped read-only vs trade-enabled; secrets
come from env/.env/keyring and are never logged.

Phase 0 status: this package is a deliberate placeholder. The risk engine, the
arming state machine, and ``kill_switch`` are NOT implemented here yet -- they
land in Phase 6 and must not be wired before then. The secret-handling and key
*scoping* primitives that back this invariant live in
:mod:`trader_mcp.config` (see :class:`~trader_mcp.config.KeyScope`,
:class:`~trader_mcp.config.ExchangeCredentials`) and the redaction filter in
:mod:`trader_mcp.logging_config`. Default every new execution surface to dry-run.
"""

from __future__ import annotations

#: Marker affirming the safe-by-default invariant for this package. Anything that
#: places, arms, or routes orders must default to dry-run until Phase 6.
SAFE_BY_DEFAULT = True

__all__ = ["SAFE_BY_DEFAULT"]
