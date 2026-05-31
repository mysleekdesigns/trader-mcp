# Exchange quirks log

The canonical, human-readable record of per-exchange behavior discovered during live
validation (PRD §6 Phase 1 exit item: *"Deep-validate Bybit; smoke-test
BloFin/Toobit/WeeX; log known quirks"*).

**How to update:** run the live validation from an allowed-region vantage point and
record findings here.

```bash
# Over a VPN, or with a proxy env var set:
uv run trader-mcp-validate --json live-report.json     # human report + JSON
TRADER_MCP_LIVE_TESTS=1 uv run pytest tests/live --live -q   # the live suite
```

The `trader-mcp-validate --json` output and the live-suite `skip` reasons are the raw
material; distill them into the per-exchange sections below. The CI job
`.github/workflows/live-validation.yml` uploads the JSON report + this file as an
artifact on each manual run.

> Status (2026-05-30): live deep-validation not yet run from an allowed region — this
> dev host and the build sandbox are geo-blocked (see the Bybit 403 quirk below). The
> machinery (proxy support, opt-in live suite, CLI, CI probe) is in place; the run is
> one command/click away once a vantage point is available.

---

## Known connectivity / environment quirks

### Bybit 403 geo-block → clean `ExchangeError`

From a geo-blocked region, Bybit's CloudFront edge returns **HTTP 403** ("...configured
to block access from your country") on public endpoints. CCXT raises a `BaseError`
subclass which the adapter maps to a clean `ExchangeError` with a redacted cause (no raw
exception, no secrets, no stack trace). Observed `details["kind"]` is `"exchange"`
(a generic `ccxt.BaseError`); when CCXT classifies the 403 as `ccxt.PermissionDenied`
instead, the kind is `"auth"` (verified offline in `tests/exchanges/test_errors.py`).
Either way the failure is typed and safe; no geo-block special-casing is warranted.

Observed (2026-05-30) from this geo-blocked dev host: the adapter-level failure path
only. `trader-mcp-validate` / the `--live` suite reach the adapter and fail fast with
`trader_mcp.errors.ExchangeError: bybit.fetch_ticker: The exchange operation failed.`
— the expected typed-error behavior. **Full live validation against real data is still
pending an allowed-region vantage point** (VPN/proxy/unblocked CI); the per-exchange
sections below remain unfilled until then.

**Escape hatches** (read-only public data only):
- A VPN to an allowed region (OS-level routing; no code change needed).
- `TRADER_MCP_HTTPS_PROXY` — HTTPS proxy (no extra dependency).
- `TRADER_MCP_SOCKS_PROXY` — SOCKS proxy (requires `uv sync --extra socks`).
- `TRADER_MCP_REQUEST_TIMEOUT_MS` — per-request timeout (default 30000).

Only one of the two proxy settings may be set at a time (CCXT rejects both).

---

## Methodology note: capability gaps read differently in the two tools

For the non-reference exchanges, a missing capability (e.g. `fetch_ohlcv` not
supported) is reported as a **pytest `skip`** by `tests/live/test_smoke_other_exchanges_live.py`
but as a **`FAIL` (quirk)** by `trader-mcp-validate`. Both are intentional: the live
suite must stay green on capability gaps (Bybit is the real gate), while the CLI report
is the more honest signal for the quirks distilled here. Cross-reference both when
filling in the sections below.

## Per-exchange findings

Fill these in from live-run output.

### Bybit (reference, `certified`)

- Status: _pending live run._
- Spot symbol probed: `BTC/USDT` · Perp probed: `BTC/USDT:USDT`.
- Funding rate: _tbd._
- Notes: _tbd._

### BloFin (`supported`)

- Status: _pending live run._
- Symbol notation: _tbd._
- Capability gaps (e.g. `fetch_ohlcv` not supported): _tbd._
- Testnet available: _tbd._

### Toobit (`supported`)

- Status: _pending live run._
- Symbol notation: _tbd._
- Capability gaps: _tbd._
- Testnet available: _tbd._

### WeeX (`supported`)

- Status: _pending live run._
- Symbol notation: _tbd._
- Capability gaps: _tbd._
- Testnet available: _tbd._
