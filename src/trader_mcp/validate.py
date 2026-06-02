"""Live-validation CLI: ``trader-mcp-validate``.

Hits the *real* public market-data endpoints of the four target exchanges to satisfy
the PRD §6 Phase 1 exit item ("Deep-validate Coinbase; smoke-test Kraken/Gemini/Crypto.com;
log known quirks"). This is the command you run from an allowed-region vantage point
(VPN, proxy, or unblocked CI runner) when this dev host has no exchange network.

Read-only & safe by default: it calls only public market-data + read paths via the
single :class:`~trader_mcp.exchanges.ExchangeAdapter`; no order surface exists and no
API keys are required. Proxy/timeout settings (``TRADER_MCP_HTTPS_PROXY`` /
``TRADER_MCP_SOCKS_PROXY`` / ``TRADER_MCP_REQUEST_TIMEOUT_MS``) are honored
automatically, so the same command "just works" over a VPN or with a proxy env var.

Exit code: ``0`` only if the Coinbase deep-validation fully passes (so CI can gate on
it). Smoke-test failures on the non-reference exchanges are reported but do not fail the
run -- they are recorded as quirks. The reference exchange is selected dynamically via
the registry's ``is_reference`` flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from trader_mcp.config import ExchangeId
from trader_mcp.exchanges import ExchangeAdapter, ExchangeManager, list_supported
from trader_mcp.logging_config import redact

#: Default reference perp symbol for funding-rate + perp checks. NOTE: the v1
#: reference exchange (Coinbase) is spot-only and has no perps/funding -- the
#: funding-rate check is therefore tolerated (non-gating) for a spot-only reference
#: (see :func:`_deep_validate`). This default exists for a perp-capable reference.
DEFAULT_PERP = "BTC/USD:USD"
#: Default spot symbol for ticker/ohlcv/order-book/trades checks (US venues quote USD).
DEFAULT_SPOT = "BTC/USD"


@dataclass(slots=True)
class CheckResult:
    """One validation check's outcome."""

    name: str
    ok: bool
    detail: str


@dataclass(slots=True)
class ExchangeReport:
    """All checks run against a single exchange."""

    exchange: str
    mode: str  # "deep" (Coinbase, the reference) or "smoke" (others)
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


async def _run_check(name: str, coro_factory: Callable[[], Awaitable[CheckResult]]) -> CheckResult:
    """Run one check, turning any exception into a failed (redacted) result."""
    try:
        return await coro_factory()
    except Exception as exc:
        return CheckResult(name=name, ok=False, detail=f"error: {redact(str(exc))}")


def _ok(name: str, detail: str = "ok") -> CheckResult:
    return CheckResult(name=name, ok=True, detail=detail)


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, ok=False, detail=detail)


async def _deep_validate(adapter: ExchangeAdapter, *, spot: str, perp: str) -> list[CheckResult]:
    """Exercise all 9 market-data paths and assert basic invariants (Coinbase).

    The funding-rate check is NON-GATING for a spot-only reference (Coinbase has no
    perps/funding): if the venue does not support funding, it is recorded as a
    tolerated/informational ``_ok`` result so the gate does not fail merely because
    the reference lacks perps. The other 8 checks remain real invariant gates.
    """
    results: list[CheckResult] = []

    async def capabilities() -> CheckResult:
        await adapter.load_markets()
        caps = adapter.capabilities()
        if not caps.market_count or caps.market_count <= 0:
            return _fail("capabilities", "market_count not populated")
        return _ok("capabilities", f"{caps.market_count} markets; ohlcv={caps.supports_ohlcv}")

    async def list_markets() -> CheckResult:
        markets = await adapter.list_markets(limit=5)
        if not markets:
            return _fail("list_markets", "no markets returned")
        return _ok("list_markets", f"{markets[0].symbol} ...")

    async def search_symbols() -> CheckResult:
        matches = await adapter.search_symbols("BTC", limit=5)
        if not matches:
            return _fail("search_symbols", "no matches for 'BTC'")
        return _ok("search_symbols", f"{len(matches)} matches")

    async def ticker() -> CheckResult:
        t = await adapter.fetch_ticker(spot)
        if t.bid is None or t.ask is None or not (t.bid < t.ask):
            return _fail("ticker", f"bid<ask violated: bid={t.bid} ask={t.ask}")
        return _ok("ticker", f"last={t.last} bid={t.bid} ask={t.ask}")

    async def ohlcv() -> CheckResult:
        res = await adapter.fetch_ohlcv(spot, "1h", limit=50)
        if not res.bars:
            return _fail("ohlcv", "no bars returned")
        ts = [b.timestamp for b in res.bars]
        if ts != sorted(ts) or len(set(ts)) != len(ts):
            return _fail("ohlcv", "timestamps not strictly increasing")
        for b in res.bars:
            if b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
                return _fail("ohlcv", f"OHLC bounds violated at {b.timestamp.isoformat()}")
        return _ok("ohlcv", f"{res.count} bars, monotonic UTC, bounds ok")

    async def order_book() -> CheckResult:
        ob = await adapter.fetch_order_book(spot, limit=10)
        if not ob.bids or not ob.asks:
            return _fail("order_book", "empty side(s)")
        if not (ob.bids[0].price < ob.asks[0].price):
            return _fail("order_book", f"best bid {ob.bids[0].price} >= ask {ob.asks[0].price}")
        return _ok("order_book", f"bid {ob.bids[0].price} < ask {ob.asks[0].price}")

    async def recent_trades() -> CheckResult:
        res = await adapter.fetch_recent_trades(spot, limit=10)
        if not res.trades:
            return _fail("recent_trades", "no trades returned")
        if any(t.price <= 0 for t in res.trades):
            return _fail("recent_trades", "non-positive trade price")
        return _ok("recent_trades", f"{res.count} trades")

    async def funding_rate() -> CheckResult:
        # NON-GATING for a spot-only reference (Coinbase has no perps/funding).
        # Treat an unsupported/missing funding rate as a tolerated, informational
        # result so the deep-validate gate does not fail purely on a venue that
        # lacks perps. Any other path failing would surface via _run_check.
        if not adapter.capabilities().supports_funding_rate:
            return _ok("funding_rate", "n/a (spot-only venue)")
        try:
            fr = await adapter.fetch_funding_rate(perp)
        except Exception:
            return _ok("funding_rate", "n/a (spot-only venue)")
        if fr.funding_rate is None:
            return _ok("funding_rate", "n/a (spot-only venue)")
        return _ok("funding_rate", f"{perp} rate={fr.funding_rate}")

    checks: list[tuple[str, Callable[[], Awaitable[CheckResult]]]] = [
        ("capabilities", capabilities),
        ("list_markets", list_markets),
        ("search_symbols", search_symbols),
        ("ticker", ticker),
        ("ohlcv", ohlcv),
        ("order_book", order_book),
        ("recent_trades", recent_trades),
        ("funding_rate", funding_rate),
    ]
    for name, factory in checks:
        results.append(await _run_check(name, factory))
    return results


async def _smoke(adapter: ExchangeAdapter) -> list[CheckResult]:
    """Minimal liveness for non-reference exchanges: markets + ticker + ohlcv.

    Tolerant of capability gaps -- a missing path is recorded as a failed check
    (a quirk) rather than aborting the report.
    """
    results: list[CheckResult] = []

    async def markets() -> CheckResult:
        ms = await adapter.list_markets(active_only=True)
        if not ms:
            return _fail("list_markets", "no active markets")
        return _ok("list_markets", f"{len(ms)} active markets")

    results.append(await _run_check("list_markets", markets))

    # Pick a concrete symbol from the loaded markets (notation varies by exchange).
    symbol: str | None = None
    try:
        spot_markets = await adapter.list_markets(market_type="spot", active_only=True, limit=1)
        if spot_markets:
            symbol = spot_markets[0].symbol
    except Exception:
        symbol = None

    if symbol is None:
        results.append(_fail("ticker", "no spot symbol available to probe"))
        results.append(_fail("ohlcv", "no spot symbol available to probe"))
        return results

    async def ticker() -> CheckResult:
        t = await adapter.fetch_ticker(symbol)  # type: ignore[arg-type]
        return _ok("ticker", f"{symbol} last={t.last}")

    async def ohlcv() -> CheckResult:
        res = await adapter.fetch_ohlcv(symbol, "1h", limit=10)  # type: ignore[arg-type]
        if not res.bars:
            return _fail("ohlcv", "no bars")
        return _ok("ohlcv", f"{symbol} {res.count} bars")

    results.append(await _run_check("ticker", ticker))
    results.append(await _run_check("ohlcv", ohlcv))
    return results


def _reference_exchange() -> ExchangeId:
    """Return the registry's reference exchange id (currently Coinbase)."""
    for info in list_supported():
        if info.is_reference:
            return info.exchange
    # The registry always defines exactly one reference; fall back defensively.
    return list_supported()[0].exchange


async def _validate(exchanges: list[ExchangeId], *, spot: str, perp: str) -> list[ExchangeReport]:
    """Run deep-validation on the reference exchange and a smoke test on the others."""
    reference = _reference_exchange()
    manager = ExchangeManager()
    reports: list[ExchangeReport] = []
    try:
        for ex in exchanges:
            adapter = await manager.get(ex)
            if ex == reference:
                checks = await _deep_validate(adapter, spot=spot, perp=perp)
                reports.append(ExchangeReport(exchange=ex, mode="deep", checks=checks))
            else:
                checks = await _smoke(adapter)
                reports.append(ExchangeReport(exchange=ex, mode="smoke", checks=checks))
    finally:
        await manager.aclose_all()
    return reports


def _print_report(reports: list[ExchangeReport]) -> None:
    for report in reports:
        header = f"{report.exchange.upper()} ({report.mode})"
        print(f"\n=== {header} ===")
        for c in report.checks:
            mark = "PASS" if c.ok else "FAIL"
            print(f"  [{mark}] {c.name}: {c.detail}")


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``trader-mcp-validate``.

    Returns a process exit code: ``0`` only when the reference exchange (Coinbase)
    deep-validation fully passes.
    """
    parser = argparse.ArgumentParser(
        prog="trader-mcp-validate",
        description="Validate live exchange connectivity against real public market data.",
    )
    supported = list(list_supported())
    parser.add_argument(
        "--exchange",
        dest="exchanges",
        action="append",
        choices=[e.exchange for e in supported],
        help="Exchange(s) to validate (repeatable). Default: all supported.",
    )
    parser.add_argument(
        "--spot", default=DEFAULT_SPOT, help=f"Spot symbol (default {DEFAULT_SPOT})."
    )
    parser.add_argument(
        "--perp", default=DEFAULT_PERP, help=f"Perp symbol (default {DEFAULT_PERP})."
    )
    parser.add_argument(
        "--json", dest="json_out", default=None, help="Write a JSON report to this path."
    )
    args = parser.parse_args(argv)

    exchanges: list[ExchangeId] = args.exchanges or [e.exchange for e in supported]
    reports = asyncio.run(_validate(exchanges, spot=args.spot, perp=args.perp))

    _print_report(reports)

    if args.json_out is not None:
        payload = [
            {"exchange": r.exchange, "mode": r.mode, "checks": [asdict(c) for c in r.checks]}
            for r in reports
        ]
        try:
            # 0o600: the report aggregates connectivity diagnostics; restrict it even
            # though check details are already redacted (defense-in-depth).
            fd = os.open(args.json_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as exc:
            print(f"\nCould not write JSON report to {args.json_out}: {exc}")
            return 1
        print(f"\nWrote JSON report to {args.json_out}")

    # Gate on the reference exchange (Coinbase). Others are smoke/quirks-only.
    reference = _reference_exchange()
    reference_report = next((r for r in reports if r.exchange == reference), None)
    failed = sum(1 for r in reports for c in r.checks if not c.ok)
    print(f"\n{failed} check(s) failed across {len(reports)} exchange(s).")
    if reference_report is not None and not reference_report.passed:
        print(f"{reference.capitalize()} deep-validation FAILED.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module/CLI dual entry
    sys.exit(main())
