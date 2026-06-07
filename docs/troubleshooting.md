# Troubleshooting

Common issues running trader-mcp and how to fix them. Most problems are one of:
the wrong Python version, a geo-blocked exchange, a missing `.env`, or a client that
hasn't reloaded its MCP config.

---

## The server won't boot

trader-mcp speaks MCP over **stdio** and blocks waiting for a client — so launching
`uvx trader-mcp` in a plain terminal and seeing it "hang" with no prompt is
**expected**, not a hang. It is meant to be launched by an MCP client, not run
interactively.

To sanity-check the install outside a client:

```bash
uvx trader-mcp --help        # should print without an import/build error
uvx --from . trader-mcp      # run from a local checkout
```

Turn up logging to see boot detail (logs go to **stderr**; stdout is the JSON-RPC
channel and must stay clean):

```bash
TRADER_MCP_LOG_LEVEL=DEBUG uvx --from . trader-mcp
```

If the failure is during dependency resolution/build, it is almost always the Python
version — see below.

## Python version: use 3.12

trader-mcp targets **Python 3.12**, and several scientific dependencies ship
**prebuilt wheels for 3.12** but not for newer interpreters.

- On **Python 3.14** (or other very new versions), `numba`/`llvmlite` fall back to
  **compiling from source**, which needs a matching LLVM toolchain and usually
  fails with a long C/LLVM build error.
- `pandas-ta` (the indicator library) is likewise validated on **3.12**.

Fix: pin the interpreter to 3.12. With `uv` this is one flag:

```bash
uvx --python 3.12 trader-mcp
# or, in a checkout:
uv python pin 3.12 && uv sync
```

In a client config, make `uvx` select 3.12 by adding `"--python", "3.12"` to the
`args` array before `trader-mcp` (or before `--from ...`).

## Exchange geo-blocking / region errors

The four supported venues — Coinbase (reference), Kraken, Gemini, Crypto.com — are
**US-accessible and USD/USDC-quoted**. If a public market-data or `sync_history`
call returns a region/`451`/`403`/"restricted location" error, your host's IP is
being geo-blocked by the exchange.

Options:

- Run from a region the exchange serves.
- Route CCXT through a proxy (next section).

`testnet` sessions additionally run a **jurisdiction check** up front, so an
ineligible venue/market is rejected at `start_session` with a clear error rather
than failing mid-order.

## Proxy configuration

If a host is geo-blocked, point CCXT at a proxy:

- **HTTPS proxy:** set `TRADER_MCP_HTTPS_PROXY=https://user:pass@host:port`.
- **SOCKS proxy:** set `TRADER_MCP_SOCKS_PROXY=socks5://host:port`. SOCKS needs the
  optional dependency — install with `uv sync --extra socks` (it pulls in
  `aiohttp_socks`). The HTTPS and SOCKS proxy settings are **mutually exclusive**.

Proxy URLs may embed `user:pass@` credentials, so they are treated as secrets:
never logged, revealed only at the CCXT construction call site.

## `.env` not found / settings not applied

Settings load with precedence **environment → `.env` → OS keyring**, and the `.env`
file is read **from the working directory of the server process** — which, under an
MCP client, is whatever directory the client launches `uvx` in (often your project
root, not your home directory).

If your `.env` seems ignored:

- Put `.env` in the directory the client runs from, **or** set the variables
  directly in the client's `env` block (see [client-setup.md](./client-setup.md)).
- Copy [`.env.example`](../.env.example) as a starting point.
- Remember the prefixes: general settings use `TRADER_MCP_`, per-exchange keys use
  `<EXCHANGE>_API_KEY` / `<EXCHANGE>_API_SECRET`.
- `.env` is gitignored — never commit it. Secrets are never logged or echoed.

## MCP client not seeing the tools

If the client connects but no trader-mcp tools appear:

- **Reload/restart the client** after editing its MCP config — Cursor and Codex
  need a restart (or an MCP-servers reload) to relaunch the subprocess.
- Confirm the entry name and command. For Claude Code, `claude mcp list` should show
  `trader-mcp` as connected; re-add with
  `claude mcp add trader-mcp -- uvx trader-mcp` if not.
- Verify the link by calling `health_check` and `get_server_status` — the latter
  reports the live `tool_count` (~46). If those work but a specific tool is missing,
  re-check you are on the current version (`get_server_status.version`).
- Check stderr (raise `TRADER_MCP_LOG_LEVEL=DEBUG`) for a boot error that aborted
  registration.

## Live / testnet orders aren't routing

This is **by design** — trader-mcp is safe-by-default:

- There is **no `live` session mode**; real-money live trading is gated and has not
  been opened.
- `place_order` on a `paper` session is **always simulated** (`simulated=true`).
- A `testnet` order only **routes** to the exchange sandbox when **all** hold: the
  key is **trade-enabled**, the venue/market is **US-eligible**, and the process-wide
  **dry-run default is off** (`TRADER_MCP_DRY_RUN`). Otherwise it is downgraded to a
  simulate, or denied with a redacted safety reason.
- `set_risk_limits` caps are enforced **before** the gate; an over-limit order is
  denied. A market order with no derivable price is denied while a notional cap is
  set (fail-closed).

See [safety.md](./safety.md) for the full gate and arming model, and `get_safety_status`
/ `get_audit_log` to inspect why an order was simulated or denied.

## Live tests are skipped

The default test suite is fully offline and deterministic (fakes/fixtures, no real
network). **Live exchange tests are opt-in and skipped by default** — they require
real network access (and, for some, credentials), so they do not run in CI or a
normal `uv run pytest`. That is expected; a green default run does not exercise a
live venue.

---

If none of the above fixes it, run with `TRADER_MCP_LOG_LEVEL=DEBUG`, capture the
stderr output, and check it against the error taxonomy — all errors surfaced across
the MCP boundary are typed and redacted (they never contain secrets).
