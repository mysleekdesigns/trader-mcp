"""Adapter cache and lifecycle management.

:class:`ExchangeManager` owns the set of live :class:`ExchangeAdapter` instances,
caching at most one per ``(exchange, testnet)`` pair behind an ``asyncio.Lock`` so
concurrent callers share a single rate-limited CCXT client. The MCP server holds
one manager for the process lifetime and closes it on shutdown.
"""

from __future__ import annotations

import asyncio

from trader_mcp.config import ExchangeId, Settings
from trader_mcp.errors import ValidationError
from trader_mcp.exchanges.adapter import ExchangeAdapter
from trader_mcp.exchanges.registry import is_supported
from trader_mcp.logging_config import get_logger

_logger = get_logger(__name__)


class ExchangeManager:
    """Process-wide cache of exchange adapters, keyed by ``(exchange, testnet)``.

    Thread-safety within a single event loop is provided by an ``asyncio.Lock``
    around the create-or-get critical section, so two concurrent :meth:`get` calls
    for the same key construct only one adapter.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        """Create an empty manager.

        Args:
            settings: Optional settings injected into every adapter created by this
                manager (defaults to the process-wide cached settings per adapter).
        """
        self._settings = settings
        self._adapters: dict[tuple[ExchangeId, bool], ExchangeAdapter] = {}
        self._lock = asyncio.Lock()

    async def get(self, exchange: ExchangeId, *, testnet: bool = False) -> ExchangeAdapter:
        """Return a cached adapter for ``(exchange, testnet)``, creating it if needed.

        Args:
            exchange: A supported trader-mcp exchange id.
            testnet: Whether to use the exchange's sandbox/testnet endpoints.

        Returns:
            A shared, ready-to-use :class:`ExchangeAdapter`.

        Raises:
            trader_mcp.errors.ValidationError: If ``exchange`` is not a supported
                exchange id. Validated before any client construction.
        """
        if not is_supported(exchange):
            raise ValidationError(
                f"Unsupported exchange: {exchange!r}",
                details={"exchange": exchange, "kind": "unsupported_exchange"},
            )

        key = (exchange, testnet)
        # Fast path: already cached.
        existing = self._adapters.get(key)
        if existing is not None:
            return existing

        async with self._lock:
            # Re-check inside the lock: another coroutine may have created it.
            existing = self._adapters.get(key)
            if existing is not None:
                return existing
            adapter = await ExchangeAdapter.create(
                exchange, testnet=testnet, settings=self._settings
            )
            self._adapters[key] = adapter
            _logger.debug("Cached new adapter for %s (testnet=%s)", exchange, testnet)
            return adapter

    async def aclose_all(self) -> None:
        """Close every cached adapter and clear the cache.

        Safe to call multiple times. Errors from individual ``aclose`` calls are
        swallowed (each adapter's :meth:`ExchangeAdapter.aclose` is already
        defensive) so one stuck client cannot block the rest of shutdown.
        """
        async with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        await asyncio.gather(*(a.aclose() for a in adapters), return_exceptions=True)
        _logger.debug("Closed %d cached adapter(s)", len(adapters))
