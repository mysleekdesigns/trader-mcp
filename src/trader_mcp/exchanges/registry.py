"""Static registry of supported exchanges and their metadata.

This is the single, small place where per-exchange differences live. The adapter
itself stays exchange-agnostic and reaches every exchange through the same CCXT
code path; any genuine quirk is encoded here (or as a CCXT capability flag) rather
than as a branch in the adapter.

PRD: Bybit is the reference exchange (validated first, ``certified`` tier); the
other three are ``supported`` tier. All four expose WebSocket streams via CCXT Pro
(used from Phase 5).
"""

from __future__ import annotations

from trader_mcp.config import ExchangeId
from trader_mcp.exchanges.models import ExchangeInfo, ReliabilityTier


class _ExchangeMeta:
    """Internal record of static metadata for one supported exchange."""

    __slots__ = ("ccxt_id", "exchange", "has_websocket", "is_reference", "name", "reliability_tier")

    exchange: ExchangeId
    ccxt_id: str
    name: str
    is_reference: bool
    reliability_tier: ReliabilityTier
    has_websocket: bool

    def __init__(
        self,
        *,
        exchange: ExchangeId,
        ccxt_id: str,
        name: str,
        is_reference: bool,
        reliability_tier: ReliabilityTier,
        has_websocket: bool,
    ) -> None:
        self.exchange = exchange
        self.ccxt_id = ccxt_id
        self.name = name
        self.is_reference = is_reference
        self.reliability_tier = reliability_tier
        self.has_websocket = has_websocket


# Deterministic order: the reference exchange (bybit) first, then the others. The
# ``ccxt_id`` matches ``ccxt.async_support.<ccxt_id>`` and is identical to the
# trader-mcp ``ExchangeId`` for all four exchanges today, but is kept separate so a
# future divergence (a renamed CCXT id) is a one-line change here.
_REGISTRY: tuple[_ExchangeMeta, ...] = (
    _ExchangeMeta(
        exchange="bybit",
        ccxt_id="bybit",
        name="Bybit",
        is_reference=True,
        reliability_tier="certified",
        has_websocket=True,
    ),
    _ExchangeMeta(
        exchange="blofin",
        ccxt_id="blofin",
        name="BloFin",
        is_reference=False,
        reliability_tier="supported",
        has_websocket=True,
    ),
    _ExchangeMeta(
        exchange="toobit",
        ccxt_id="toobit",
        name="Toobit",
        is_reference=False,
        reliability_tier="supported",
        has_websocket=True,
    ),
    _ExchangeMeta(
        exchange="weex",
        ccxt_id="weex",
        name="WeeX",
        is_reference=False,
        reliability_tier="supported",
        has_websocket=True,
    ),
)

_BY_ID: dict[str, _ExchangeMeta] = {meta.exchange: meta for meta in _REGISTRY}


def _to_info(meta: _ExchangeMeta) -> ExchangeInfo:
    return ExchangeInfo(
        exchange=meta.exchange,
        ccxt_id=meta.ccxt_id,
        name=meta.name,
        is_reference=meta.is_reference,
        reliability_tier=meta.reliability_tier,
        has_websocket=meta.has_websocket,
    )


def list_supported() -> list[ExchangeInfo]:
    """Return metadata for all supported exchanges.

    Returns:
        A deterministically-ordered list (Bybit, the reference exchange, first).
    """
    return [_to_info(meta) for meta in _REGISTRY]


def is_supported(exchange: str) -> bool:
    """Return whether ``exchange`` is a supported trader-mcp exchange id.

    Args:
        exchange: A candidate exchange id (case-sensitive; trader-mcp ids are
            lowercase).
    """
    return exchange in _BY_ID


def get_exchange_meta(exchange: ExchangeId) -> ExchangeInfo:
    """Return static metadata for a supported exchange.

    Args:
        exchange: A supported trader-mcp exchange id.

    Returns:
        The :class:`ExchangeInfo` for ``exchange``.

    Raises:
        KeyError: If ``exchange`` is not in the registry. Callers that accept
            untrusted input should guard with :func:`is_supported` first (the
            adapter/manager do).
    """
    return _to_info(_BY_ID[exchange])


def ccxt_id_for(exchange: ExchangeId) -> str:
    """Return the CCXT client id for a supported exchange id."""
    return _BY_ID[exchange].ccxt_id
