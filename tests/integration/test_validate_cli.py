"""Offline tests for the ``trader-mcp-validate`` live-validation CLI (``trader_mcp.validate``).

The CLI normally hits real exchange endpoints; here we replay SANITIZED cassettes
through the same :class:`~trader_mcp.exchanges.ExchangeManager` seam so the entire
deep-validate (Coinbase reference) + smoke (Kraken) + reporting + JSON-output + exit-code
flow is exercised deterministically and offline. This converts a previously 0%-covered
module into a genuinely tested path without any network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import trader_mcp.exchanges.adapter as adapter_module
import trader_mcp.validate as validate_module
from tests.integration._cassettes import CassetteCcxt
from trader_mcp.exchanges import ExchangeAdapter
from trader_mcp.validate import (
    CheckResult,
    ExchangeReport,
    _deep_validate,
    _reference_exchange,
    _run_check,
    _smoke,
    _validate,
    main,
)

pytestmark = pytest.mark.usefixtures("no_credentials")


@pytest.fixture
def cassette_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the manager build cassette-backed clients keyed by exchange id.

    The factory receives the ccxt id (== exchange id for our four venues) and returns
    the matching recorded cassette so ``_validate`` can run the full multi-exchange flow.
    """

    def factory(exchange_id: str, config: dict[str, object]) -> CassetteCcxt:
        return CassetteCcxt(exchange_id)

    monkeypatch.setattr(adapter_module, "_create_ccxt_client", factory)


async def test_deep_validate_coinbase_all_checks_pass(cassette_clients: None) -> None:

    adapter = await ExchangeAdapter.create("coinbase")
    try:
        checks = await _deep_validate(adapter, spot="BTC/USD", perp="BTC/USD:USD")
    finally:
        await adapter.aclose()
    names = {c.name for c in checks}
    assert {"capabilities", "ticker", "ohlcv", "order_book", "recent_trades"} <= names
    assert all(c.ok for c in checks), [(c.name, c.detail) for c in checks if not c.ok]
    # Spot-only reference: funding-rate is tolerated (non-gating) and reported n/a.
    funding = next(c for c in checks if c.name == "funding_rate")
    assert funding.ok
    assert "n/a" in funding.detail


async def test_smoke_kraken_passes(cassette_clients: None) -> None:

    adapter = await ExchangeAdapter.create("kraken")
    try:
        checks = await _smoke(adapter)
    finally:
        await adapter.aclose()
    assert {c.name for c in checks} == {"list_markets", "ticker", "ohlcv"}
    assert all(c.ok for c in checks), [(c.name, c.detail) for c in checks if not c.ok]


async def test_validate_runs_deep_for_reference_and_smoke_for_others(
    cassette_clients: None,
) -> None:
    reports = await _validate(["coinbase", "kraken"], spot="BTC/USD", perp="BTC/USD:USD")
    by_exchange = {r.exchange: r for r in reports}
    assert by_exchange["coinbase"].mode == "deep"
    assert by_exchange["kraken"].mode == "smoke"
    assert all(r.passed for r in reports)


def test_reference_exchange_is_coinbase() -> None:
    assert _reference_exchange() == "coinbase"


def test_exchange_report_passed_property() -> None:

    ok_report = ExchangeReport(
        exchange="coinbase",
        mode="deep",
        checks=[CheckResult("a", True, "ok"), CheckResult("b", True, "ok")],
    )
    bad_report = ExchangeReport(
        exchange="coinbase",
        mode="deep",
        checks=[CheckResult("a", True, "ok"), CheckResult("b", False, "boom")],
    )
    assert ok_report.passed is True
    assert bad_report.passed is False


def test_main_returns_zero_when_reference_passes(
    cassette_clients: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--exchange", "coinbase", "--exchange", "kraken"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "COINBASE (deep)" in out
    assert "KRAKEN (smoke)" in out
    assert "[PASS]" in out


def test_main_writes_json_report(
    cassette_clients: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = tmp_path / "report.json"
    rc = main(["--exchange", "coinbase", "--json", str(out_path)])
    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload[0]["exchange"] == "coinbase"
    assert payload[0]["mode"] == "deep"
    assert all("name" in c and "ok" in c and "detail" in c for c in payload[0]["checks"])
    assert "Wrote JSON report" in capsys.readouterr().out


def test_main_returns_one_when_reference_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing reference deep-validate must gate the exit code to 1."""

    async def fake_validate(exchanges: list[str], *, spot: str, perp: str) -> list[ExchangeReport]:
        return [
            ExchangeReport(
                exchange="coinbase",
                mode="deep",
                checks=[CheckResult("ticker", False, "bid<ask violated")],
            )
        ]

    monkeypatch.setattr(validate_module, "_validate", fake_validate)
    rc = main(["--exchange", "coinbase"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED" in out


def test_main_json_write_failure_returns_one(
    cassette_clients: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unwritable JSON path is reported and gates the exit code to 1."""
    # A path whose parent does not exist triggers an OSError in os.open.
    bad_path = tmp_path / "missing_dir" / "report.json"
    rc = main(["--exchange", "coinbase", "--json", str(bad_path)])
    assert rc == 1
    assert "Could not write JSON report" in capsys.readouterr().out


def test_deep_validate_reports_failures(
    cassette_clients: None,
) -> None:
    """A check whose invariant is violated is recorded as a failed CheckResult."""

    async def run() -> list:
        adapter = await ExchangeAdapter.create("coinbase")
        try:
            # Force the ticker invariant to fail by swapping the cassette ticker so
            # bid >= ask, exercising the _fail branch.
            original = adapter.fetch_ticker

            async def bad_ticker(symbol: str):
                t = await original(symbol)
                return t.model_copy(update={"bid": 99999.0, "ask": 1.0})

            adapter.fetch_ticker = bad_ticker  # type: ignore[method-assign]
            return await _deep_validate(adapter, spot="BTC/USD", perp="BTC/USD:USD")
        finally:
            await adapter.aclose()

    import asyncio

    checks = asyncio.run(run())
    ticker = next(c for c in checks if c.name == "ticker")
    assert ticker.ok is False
    assert "bid<ask" in ticker.detail


async def test_run_check_redacts_exception_secret() -> None:
    """A raised exception is turned into a failed, REDACTED CheckResult (no secret leak)."""
    from tests._fakes import SECRET_TOKEN, assert_no_secret

    async def boom() -> CheckResult:
        raise RuntimeError(f"upstream blew up apiKey={SECRET_TOKEN}")

    result = await _run_check("ticker", boom)
    assert result.ok is False
    assert result.detail.startswith("error:")
    assert_no_secret(result.detail)


async def test_smoke_handles_no_spot_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no spot symbol is discoverable, ticker/ohlcv are recorded as failures."""

    class _NoSpotAdapter:
        async def list_markets(self, **kwargs: object) -> list:
            # Non-empty for the markets() liveness check, empty for the spot probe.
            return [] if kwargs.get("market_type") == "spot" else ["something"]

    checks = await _smoke(_NoSpotAdapter())  # type: ignore[arg-type]
    by_name = {c.name: c for c in checks}
    assert by_name["list_markets"].ok is True
    assert by_name["ticker"].ok is False
    assert by_name["ohlcv"].ok is False
    assert "no spot symbol" in by_name["ticker"].detail


async def test_smoke_reports_empty_markets() -> None:
    """An exchange returning no active markets is recorded as a failed smoke check."""

    class _EmptyAdapter:
        async def list_markets(self, **kwargs: object) -> list:
            return []

    checks = await _smoke(_EmptyAdapter())  # type: ignore[arg-type]
    markets = next(c for c in checks if c.name == "list_markets")
    assert markets.ok is False
    assert "no active markets" in markets.detail
