# Client setup

How to connect trader-mcp to an MCP client (Claude Code, Cursor, Codex), configure
env vars, set up **read-only** exchange keys, and verify the connection.

trader-mcp speaks **MCP over stdio**: the client launches the server as a subprocess
(`uvx trader-mcp`) and talks JSON-RPC over stdin/stdout. You do not run a long-lived
server yourself — the client owns its lifecycle. `uvx` fetches and runs the
published package; no manual `pip install` is needed.

> Requires **Python 3.12**. See [troubleshooting.md](./troubleshooting.md) if `uvx`
> picks a different interpreter.

---

## Claude Code

One command:

```bash
claude mcp add trader-mcp -- uvx trader-mcp
```

To pass env vars (e.g. a default exchange or log level):

```bash
claude mcp add trader-mcp \
  --env TRADER_MCP_LOG_LEVEL=INFO \
  --env TRADER_MCP_DEFAULT_EXCHANGE=coinbase \
  -- uvx trader-mcp
```

Run from a local checkout instead of the published package:

```bash
claude mcp add trader-mcp-local -- uvx --from /absolute/path/to/trader-mcp trader-mcp
```

Verify it is wired:

```bash
claude mcp list           # trader-mcp should appear as "connected"
```

Then, in a session, ask the assistant to call `health_check` and
`get_server_status` (see [Verifying the connection](#verifying-the-connection)).

## Cursor

Edit `~/.cursor/mcp.json` (global) — or `.cursor/mcp.json` in a project — and add a
`trader-mcp` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "trader-mcp": {
      "command": "uvx",
      "args": ["trader-mcp"],
      "env": {
        "TRADER_MCP_LOG_LEVEL": "INFO",
        "TRADER_MCP_DEFAULT_EXCHANGE": "coinbase"
      }
    }
  }
}
```

Restart Cursor (or reload the MCP servers from Settings → MCP). The server's tools,
resources, and prompts then appear in the MCP panel.

## Codex

Codex uses TOML. Edit `~/.codex/config.toml` and add an `mcp_servers` table:

```toml
[mcp_servers.trader-mcp]
command = "uvx"
args = ["trader-mcp"]

[mcp_servers.trader-mcp.env]
TRADER_MCP_LOG_LEVEL = "INFO"
TRADER_MCP_DEFAULT_EXCHANGE = "coinbase"
```

Restart Codex so it relaunches the MCP server.

---

## Environment variables

Configuration precedence is **environment → `.env` → OS keyring**. The server reads
its general settings from the `TRADER_MCP_` prefix; per-exchange credentials use an
`<EXCHANGE>_` prefix. See [`.env.example`](../.env.example) for the full list.

| Variable | Purpose | Default |
| --- | --- | --- |
| `TRADER_MCP_LOG_LEVEL` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/...). Logs go to stderr; stdout stays a clean JSON-RPC channel. | `INFO` |
| `TRADER_MCP_DEFAULT_EXCHANGE` | Default exchange when a tool omits one (`coinbase`/`kraken`/`gemini`/`cryptocom`). | `coinbase` |
| `TRADER_MCP_DATA_DIR` | Root of the local DuckDB+Parquet OHLCV cache. | `.trader_mcp_data` |
| `TRADER_MCP_DRY_RUN` | Process-wide dry-run default for execution. Keep on unless you intend testnet routing. | on |
| `TRADER_MCP_REQUEST_TIMEOUT_MS` | Per-request CCXT timeout. | `30000` |
| `TRADER_MCP_HTTPS_PROXY` | Optional HTTPS proxy URL for CCXT (geo-blocked hosts). | _(unset)_ |
| `TRADER_MCP_SOCKS_PROXY` | Optional SOCKS proxy URL (needs `uv sync --extra socks`); mutually exclusive with the HTTPS proxy. | _(unset)_ |

You can set these in the client's `env` block (shown above) or in a `.env` file in
the working directory. **Market data, backtesting, and paper trading need no
credentials at all** — those paths are fully offline / public-read.

## Read-only exchange keys

You only need credentials to (a) read private account data on a real venue or
(b) route orders to a testnet sandbox. For research, authoring, backtesting, and
paper trading, **add no keys**.

When you do add keys, **scope them read-only** at the exchange unless you
specifically intend testnet routing. trader-mcp distinguishes read-only vs
trade-enabled keys, and the safety gate refuses to route an order without a
trade-enabled key plus an off dry-run flag.

Per-exchange variables follow `<EXCHANGE>_API_KEY` / `<EXCHANGE>_API_SECRET`:

```bash
# Read-only Coinbase (the reference exchange). Same pattern for KRAKEN_, GEMINI_, CRYPTOCOM_.
COINBASE_API_KEY=...
COINBASE_API_SECRET=...
```

Put these in `.env` (gitignored) or the OS keyring — **never inline real keys into a
committed `.mcp.json`**, and never paste them into a chat. Secrets are wrapped so
they are never logged, printed, or echoed in any tool output; the safety audit log
redacts free-text detail. See [safety.md](./safety.md) for key scoping and the
arming/gate model.

---

## Verifying the connection

After the client reports the server connected, confirm the link end-to-end by
calling two read-only tools:

- **`health_check`** → returns `{status: "ok", version, timestamp}`.
- **`get_server_status`** → returns the server name, version, transport (`stdio`),
  uptime, and the live `tool_count` (~46).

A natural-language prompt works too: *"Call the trader-mcp health_check tool and tell
me the version and how many tools are registered."* If both succeed, you are wired up.

Next, try `list_exchanges` (no network) and `check_clock_skew` for one exchange (a
public read that confirms outbound connectivity and a sane host clock). Then follow
[workflows.md](./workflows.md) to sync data, author a strategy, and backtest. If
anything fails, see [troubleshooting.md](./troubleshooting.md).
