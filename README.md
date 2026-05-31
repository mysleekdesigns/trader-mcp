# trader-mcp

> A terminal-first, AI-native crypto trading platform delivered as a **Model Context Protocol (MCP) server**.
> Connect your AI assistant (Claude Code, Codex, Cursor) over MCP, **prompt your way to a strategy**, **backtest it on real market data**, and **deploy to Bybit, BloFin, Toobit, or WeeX** — all from your editor or chat.

**One-liner:** *Prompt a strategy, backtest it on real data, deploy it to your exchange — without leaving your editor.*

> **Status:** Pre-alpha. **Phase 0 (foundations & scaffolding)** is in place: a bootable MCP server over stdio with `health_check` + `get_server_status`, typed config, redacting logging, an error taxonomy, and the full toolchain. Market data, strategies, backtesting, and execution arrive in later phases — see [`PRD.md`](./PRD.md) §6.

---

## What it is

`trader-mcp` is for **developers and "vibe traders"** who live in a terminal/editor and want to drive the entire strategy lifecycle — research → author → backtest → paper → (later, gated) live — through natural-language prompts to an AI assistant, with deterministic, typed tools doing the real work underneath. An AI client connects over MCP stdio; the server exposes typed tools, resources, and prompts.

Target exchanges (all via one CCXT adapter): **Bybit** (reference), **BloFin**, **Toobit**, **WeeX**.

## Install & run

`trader-mcp` is distributed via [`uv`](https://github.com/astral-sh/uv) / `uvx`.

```bash
# Run the published server (once on PyPI):
uvx trader-mcp

# Run from a local checkout (Phase 0 exit check):
uvx --from . trader-mcp
```

The server speaks **MCP over stdio** and blocks waiting for a client — that is expected.

## Connecting an MCP client

Add `trader-mcp` to your client's MCP server config. Example (`.mcp.json`, also see [`.mcp.json.example`](./.mcp.json.example)):

```json
{
  "mcpServers": {
    "trader-mcp": {
      "command": "uvx",
      "args": ["trader-mcp"],
      "env": {
        "TRADER_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

- **Claude Code:** `claude mcp add trader-mcp -- uvx trader-mcp`
- **Cursor / Codex:** point the client's MCP config at the command above.

Once connected, the client can call `health_check` and `get_server_status`. More tools land per phase (see `PRD.md` §5.2).

## Configuration & secrets

Configuration is loaded with precedence **environment → `.env` → OS keyring**. See [`.env.example`](./.env.example) for the supported variables. Secrets (per-exchange API keys) are wrapped so they are **never logged or printed**; every log record passes through a redaction filter.

| Variable | Purpose |
| --- | --- |
| `TRADER_MCP_LOG_LEVEL` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/...). Default `INFO`. |
| `TRADER_MCP_DEFAULT_EXCHANGE` | Default exchange when a tool omits one. Default `bybit`. |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Optional read-only Bybit credentials (same pattern for `BLOFIN_`, `TOOBIT_`, `WEEX_`). |

## Development

This repo uses Python 3.12 managed with `uv`.

```bash
uv sync                      # install deps into .venv
uv run trader-mcp            # run the MCP server over stdio
uvx --from . trader-mcp      # run as an installed entry point (Phase 0 exit check)

uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyright               # type-check (or: uv run mypy src)

uv run pytest                # full test suite
uv run pytest path/to/test_file.py::test_name   # a single test

uv run pre-commit install    # enable lint/format on commit
```

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full developer guide, architecture invariants, and lane discipline.

## Safety & disclaimers

`trader-mcp` is **safe-by-default**: strategy specs are data (never executable code), order placement is **dry-run** unless explicitly armed, and real-money live trading is gated behind explicit arming + risk limits and does not land until a dedicated later phase.

**Not financial advice.** This software is provided "as is", without warranty of any kind. Trading cryptocurrencies carries substantial risk of loss. You assume all responsibility and risk for any use of this software, including any orders it places. The authors are not liable for any losses. Nothing here is a recommendation to buy or sell any asset.

## License

[MIT](./LICENSE) © 2026 Simon Lacey
