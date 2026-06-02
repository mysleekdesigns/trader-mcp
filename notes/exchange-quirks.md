# Exchange quirks log

The canonical, human-readable record of per-exchange behavior discovered during live
validation (PRD §6 Phase 1 exit item: *"Deep-validate Coinbase; smoke-test
Kraken/Gemini/Crypto.com; log known quirks"*).

**How to update:** run the live validation from a networked, US-accessible vantage
point and record findings here.

```bash
# From a host with exchange network access (or with a proxy env var set):
uv run trader-mcp-validate --json live-report.json     # human report + JSON
TRADER_MCP_LIVE_TESTS=1 uv run pytest tests/live --live -q   # the live suite
```

The `trader-mcp-validate --json` output and the live-suite `skip` reasons are the raw
material; distill them into the per-exchange sections below. The CI job
`.github/workflows/live-validation.yml` uploads the JSON report + this file as an
artifact on each manual run.

> Status (2026-06-02): live validation against the US set has **not** been run yet —
> this build sandbox has no exchange network access, so the run is deferred to a
> networked vantage point. The machinery (proxy support, opt-in live suite, CLI, CI
> probe) is in place; the run is one command/click away once a networked host is
> available. Until then the per-exchange sections below are placeholders.

---

## Why the offshore four were dropped (historical context)

The original target set was offshore (Bybit/BloFin/Toobit/WeeX). From a geo-blocked
region, Bybit's CloudFront edge returned **HTTP 403** ("...configured to block access
from your country") on public endpoints; the offshore venues were broadly unreachable
or unsuitable for US-accessible distribution. The adapter handled the 403 cleanly —
CCXT raised a `BaseError` subclass that the adapter mapped to a redacted
`ExchangeError` (no raw exception, no secrets, no stack trace), so no geo-block
special-casing was ever warranted. The project re-targeted to the **US-accessible**
set below (Coinbase/Kraken/Gemini/Crypto.com), with Coinbase as the new reference.
The typed-error behavior verified offline in `tests/exchanges/test_errors.py` still
applies to any venue that blocks/denies a request.

## Known connectivity / environment quirks (new set)

- _Pending live run._ This build sandbox has no exchange network, so no real
  connectivity behavior has been observed against the US venues yet.

**Escape hatches** (read-only public data only):
- A networked host in an allowed region (OS-level routing; no code change needed).
- `TRADER_MCP_HTTPS_PROXY` — HTTPS proxy (no extra dependency).
- `TRADER_MCP_SOCKS_PROXY` — SOCKS proxy (requires `uv sync --extra socks`).
- `TRADER_MCP_REQUEST_TIMEOUT_MS` — per-request timeout (default 30000).

Only one of the two proxy settings may be set at a time (CCXT rejects both).

---

## Methodology note: capability gaps read differently in the two tools

For the non-reference exchanges, a missing capability (e.g. `fetch_ohlcv` not
supported) is reported as a **pytest `skip`** by `tests/live/test_smoke_other_exchanges_live.py`
but as a **`FAIL` (quirk)** by `trader-mcp-validate`. Both are intentional: the live
suite must stay green on capability gaps (Coinbase is the real gate), while the CLI
report is the more honest signal for the quirks distilled here. Cross-reference both
when filling in the sections below.

One deliberate exception: the reference exchange (**Coinbase**) is **spot-only** and
has **no perp/funding surface**. Both the `trader-mcp-validate` deep-validate and the
live `test_funding_rate_on_perp` treat a missing funding rate as **tolerated**
(non-gating / `skip`) rather than a failure, so the reference deep-validate does not
fail purely because the venue lacks perps. The other 8 market-data checks remain real
invariant gates.

## Per-exchange findings

Fill these in from live-run output.

### Coinbase (reference, `certified`)

- Status: _pending live run._
- Spot symbol probed: `BTC/USD` · Perp probed: `BTC/USD:USD` (n/a — spot-only venue).
- Funding rate: _n/a (spot-only)._
- Notes: _tbd._

### Kraken (`certified`)

- Status: _pending live run._
- Symbol notation: _tbd._
- Capability gaps (e.g. `fetch_ohlcv`/funding not supported): _tbd._
- Testnet available: _tbd._

### Gemini (`supported`)

- Status: _pending live run._
- Symbol notation: _tbd._
- Capability gaps: _tbd._
- Testnet available: _tbd._

### Crypto.com (`supported`)

- Status: _pending live run._
- Symbol notation: _tbd._
- Capability gaps: _tbd._
- Testnet available: _tbd._
