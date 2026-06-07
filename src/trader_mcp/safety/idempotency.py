"""Deterministic client order IDs and an in-process dedupe registry.

A client order id (COID) lets the exchange and our runtime recognize a retried
submission as the *same* order instead of placing a duplicate. To keep that
property testable and reproducible, COIDs here are **deterministic**: derived from
a stable hash of (session, symbol, side, sequence) with NO wall-clock time and NO
randomness. The caller supplies a monotonic per-session ``seq`` to distinguish
genuinely distinct orders within a session; identical inputs always yield the same
id, which is exactly what makes a safe retry idempotent.

Self-contained: depends only on :mod:`trader_mcp.errors`.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

from trader_mcp.errors import ValidationError

#: Stable prefix so COIDs are recognizable in logs/exchange dashboards.
COID_PREFIX: Final[str] = "tmcp"

#: Hex digest length of the COID body. 24 hex chars (96 bits) is collision-safe
#: across realistic session/order volumes while staying within exchange COID
#: length limits (most cap around 32-36 chars including the prefix).
_DIGEST_LEN: Final[int] = 24

OrderSide = Literal["buy", "sell"]


def make_client_order_id(
    *,
    session_id: str,
    symbol: str,
    side: OrderSide | str,
    seq: int | None = None,
) -> str:
    """Build a deterministic, collision-resistant client order id.

    The id is ``<prefix>-<hex>`` where ``<hex>`` is a BLAKE2b digest of the
    normalized inputs. No time or randomness is used, so the same inputs always
    produce the same id -- a retried submission with the same ``seq`` dedupes
    cleanly, and tests are reproducible.

    Args:
        session_id: Stable identifier of the trading session.
        symbol: Market symbol, e.g. ``"BTC/USD"``.
        side: ``"buy"`` or ``"sell"``.
        seq: Monotonic per-session counter the caller maintains. Distinct orders
            within a session MUST pass distinct ``seq`` values; ``None`` is
            treated as ``0`` (use only for a session's single/first order).

    Returns:
        A client order id string, e.g. ``"tmcp-1a2b3c..."``.

    Raises:
        ValidationError: if ``session_id`` or ``symbol`` is empty, ``side`` is not
            buy/sell, or ``seq`` is negative.
    """
    sid = session_id.strip()
    sym = symbol.strip()
    normalized_side = str(side).strip().lower()
    resolved_seq = 0 if seq is None else seq

    if not sid:
        raise ValidationError("session_id must be non-empty for a client order id")
    if not sym:
        raise ValidationError("symbol must be non-empty for a client order id")
    if normalized_side not in ("buy", "sell"):
        raise ValidationError(
            f"side must be 'buy' or 'sell', got {side!r}",
            details={"side": normalized_side},
        )
    if resolved_seq < 0:
        raise ValidationError(
            f"seq must be non-negative, got {resolved_seq}",
            details={"seq": resolved_seq},
        )

    # NUL-separated so the field boundaries are unambiguous (e.g. so that
    # ("ab", "c") and ("a", "bc") cannot collide). hashlib only, no time/random.
    payload = "\x00".join((sid, sym, normalized_side, str(resolved_seq)))
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
    return f"{COID_PREFIX}-{digest[:_DIGEST_LEN]}"


class IdempotencyRegistry:
    """In-process record of seen client order ids, to dedupe retried submissions.

    The execution runtime checks :meth:`seen` before placing an order and calls
    :meth:`record` once it is accepted. A retried submission that produces the same
    deterministic COID (see :func:`make_client_order_id`) is then recognized and
    skipped rather than placed twice. Process-local and not persisted -- a fresh
    process starts empty; cross-process dedupe is a Phase-6 concern.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, client_order_id: str) -> bool:
        """Return True if ``client_order_id`` has already been recorded."""
        return client_order_id in self._seen

    def record(self, client_order_id: str) -> bool:
        """Record ``client_order_id`` as submitted.

        Returns:
            ``True`` if this was a new id (first submission), ``False`` if it was
            already present (a duplicate/retry the caller should skip).
        """
        if client_order_id in self._seen:
            return False
        self._seen.add(client_order_id)
        return True

    def clear(self) -> None:
        """Forget all recorded ids (e.g. on session teardown)."""
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)
