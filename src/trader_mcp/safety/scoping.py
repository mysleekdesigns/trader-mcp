"""Key-scope and jurisdiction enforcement for the safe-by-default gate.

INVARIANT (safety): a read-only credential must be *structurally* incapable of
placing/canceling orders. Any trade-routing path calls :func:`require_trade_scope`
first; a read-only scope raises a typed :class:`~trader_mcp.errors.SafetyError`
before any exchange call is constructed.

Jurisdiction (PRD §5, §4): v1 targets only venues legally accessible to US
persons -- Coinbase, Kraken, Gemini, Crypto.com. US spot quotes are USD/USDC and
US perps are limited to CFTC-regulated products (Coinbase Derivatives, Kraken
Futures). :func:`check_jurisdiction` enforces the venue/market allowlist and
returns a disclaimer string to surface to the client.

This module depends ONLY on :mod:`trader_mcp.config` and :mod:`trader_mcp.errors`
so the safety package stays self-contained and importable without the execution,
exchange, or server layers (which are written in parallel).
"""

from __future__ import annotations

from typing import Final, Literal

from trader_mcp.config import KeyScope
from trader_mcp.errors import SafetyError

#: Market types the gate understands. ``spot`` and ``swap`` (perpetual) only in v1.
MarketType = Literal["spot", "swap"]

#: Exchanges legally accessible to US persons in v1 (CCXT ids). Coinbase is the
#: reference venue; the offshore four (Bybit/BloFin/Toobit/WeeX) are out of scope.
US_ELIGIBLE_EXCHANGES: Final[frozenset[str]] = frozenset(
    {"coinbase", "kraken", "gemini", "cryptocom"}
)

#: Of the US-eligible venues, those that offer CFTC-regulated perpetual/futures
#: products to US persons. Spot is allowed on all eligible venues; perps (``swap``)
#: are restricted to this subset.
US_PERP_EXCHANGES: Final[frozenset[str]] = frozenset({"coinbase", "kraken"})

#: Standing disclaimer surfaced on every routed (real-venue) decision. Not legal
#: or financial advice; the user assumes all risk.
JURISDICTION_DISCLAIMER: Final[str] = (
    "US-eligible venues only (Coinbase, Kraken, Gemini, Crypto.com); USD/USDC "
    "quotes; US perps limited to CFTC-regulated products. Not financial advice; "
    "you assume all risk."
)


def require_trade_scope(key_scope: KeyScope) -> None:
    """Assert that ``key_scope`` is trade-enabled, else fail closed.

    Called before any order-routing path. A read-only credential must never reach
    an exchange ``createOrder`` call, so this raises rather than returning a flag.

    Args:
        key_scope: The declared capability of the credential in use.

    Raises:
        SafetyError: if ``key_scope`` is not :attr:`KeyScope.TRADE_ENABLED`. The
            message carries no secret material (the scope is an enum, never a key).
    """
    if key_scope is not KeyScope.TRADE_ENABLED:
        raise SafetyError(
            "read-only credential cannot place or cancel orders; a trade-enabled "
            "key scope is required",
            details={"key_scope": str(key_scope), "required": str(KeyScope.TRADE_ENABLED)},
        )


def check_jurisdiction(exchange: str, market_type: MarketType) -> str:
    """Validate venue/market eligibility for US persons and return a disclaimer.

    Enforces the v1 allowlist: the exchange must be US-eligible, and a perpetual
    (``swap``) market is only permitted on a CFTC-regulated futures venue.

    Args:
        exchange: CCXT exchange id (case-insensitive).
        market_type: ``"spot"`` or ``"swap"``.

    Returns:
        The :data:`JURISDICTION_DISCLAIMER` string when eligible.

    Raises:
        SafetyError: if the exchange is not US-eligible, or a ``swap`` market is
            requested on a venue without CFTC-regulated perps.
    """
    normalized = exchange.strip().lower()
    if normalized not in US_ELIGIBLE_EXCHANGES:
        raise SafetyError(
            f"exchange {normalized!r} is not US-eligible in v1; permitted venues: "
            f"{sorted(US_ELIGIBLE_EXCHANGES)}",
            details={"exchange": normalized, "eligible": sorted(US_ELIGIBLE_EXCHANGES)},
        )
    if market_type == "swap" and normalized not in US_PERP_EXCHANGES:
        raise SafetyError(
            f"perpetual (swap) trading is not US-eligible on {normalized!r}; "
            f"US-legal perps are limited to CFTC-regulated venues: "
            f"{sorted(US_PERP_EXCHANGES)}",
            details={"exchange": normalized, "perp_eligible": sorted(US_PERP_EXCHANGES)},
        )
    return JURISDICTION_DISCLAIMER
