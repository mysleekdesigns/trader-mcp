# trader-mcp

> A terminal-first, AI-native crypto trading platform delivered as a **Model Context Protocol (MCP) server**.
> Connect your AI assistant (Claude Code, Codex, Cursor) over MCP, **prompt your way to a strategy**, **backtest it on real market data**, and **paper/testnet trade on Coinbase, Kraken, Gemini, or Crypto.com** — all from your editor or chat.

**One-liner:** *Prompt a strategy, backtest it on real data, paper-trade it on your exchange — without leaving your editor.*

> **Status:** v0.1.0 — Phases 0–6 complete. The server boots over MCP stdio and exposes **~46 typed tools** across the full strategy lifecycle: connectivity/admin, market data & discovery, historical data sync, declarative strategy authoring, backtesting & optimization, paper/testnet execution, portfolio/analytics, and the safety guardrails (`set_risk_limits`, `arm_live_trading`, `kill_switch`).
>
> **Real-money live trading is gated and deferred.** The live execution wall is intentionally up: `place_order` is dry-run / paper-by-default, testnet routing is gated behind trade-enabled keys + jurisdiction + an off dry-run flag, and there is no `live` session mode yet. See [`PRD.md`](./PRD.md) §6 for the phased plan and [`docs/safety.md`](./docs/safety.md) for the safety model.

---

## What it is

`trader-mcp` is for **developers and "vibe traders"** who live in a terminal/editor and want to drive the entire strategy lifecycle — research → author → backtest → paper/testnet → (later, gated) live — through natural-language prompts to an AI assistant, with deterministic, typed tools doing the real work underneath. An AI client connects over MCP stdio; the server exposes typed tools, resources, and prompts.

Two invariants drive the design:

1. **One interpreter for backtest and live.** The same event-driven spec interpreter consumes historical bars (backtest) and streaming/replayed bars (paper/live), guaranteeing backtest↔live parity. There is no separate live strategy engine.
2. **Safe-by-default execution.** Strategy specs are **data, never code** — rule expressions run through a sandboxed evaluator with a whitelisted set of indicators/operators. Order placement is **dry-run / paper unless explicitly gated**, and real-money live trading sits behind `arm_live_trading` + per-strategy risk limits and has not been opened.

Target exchanges (all via one CCXT adapter, all US-accessible and USD/USDC-quoted): **Coinbase** (reference), **Kraken**, **Gemini**, **Crypto.com**.

## Install & run

`trader-mcp` is distributed via [`uv`](https://github.com/astral-sh/uv) / `uvx`.

```bash
# Run the published server (once on PyPI):
uvx trader-mcp

# Run from a local checkout:
uvx --from . trader-mcp
```

The server speaks **MCP over stdio** and blocks waiting for a client — that is expected. Requires Python 3.12 (see [Troubleshooting](./docs/troubleshooting.md) for why).

## Connecting an MCP client

Add `trader-mcp` to your client's MCP server config. Quick start for Claude Code:

```bash
claude mcp add trader-mcp -- uvx trader-mcp
```

Or wire it manually (`.mcp.json`, also see [`.mcp.json.example`](./.mcp.json.example)):

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

Once connected, the client can call `health_check` and `get_server_status` to confirm the link, then `list_exchanges`, `list_markets`, `sync_history`, `create_strategy`, `run_backtest`, and so on. Per-client setup (Claude Code, Cursor, Codex), env vars, and read-only key setup are in [`docs/client-setup.md`](./docs/client-setup.md).

## Configuration & secrets

Configuration is loaded with precedence **environment → `.env` → OS keyring**. See [`.env.example`](./.env.example) for the supported variables. Secrets (per-exchange API keys) are wrapped so they are **never logged or printed**; every log record passes through a redaction filter, and the safety audit log redacts free-text detail.

| Variable | Purpose |
| --- | --- |
| `TRADER_MCP_LOG_LEVEL` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/...). Default `INFO`. |
| `TRADER_MCP_DEFAULT_EXCHANGE` | Default exchange when a tool omits one. Default `coinbase`. |
| `TRADER_MCP_DRY_RUN` | Process-wide dry-run default for execution (keep on unless you intend testnet routing). |
| `COINBASE_API_KEY` / `COINBASE_API_SECRET` | Optional **read-only** Coinbase credentials (same pattern for `KRAKEN_`, `GEMINI_`, `CRYPTOCOM_`). |

For market data, backtesting, and paper trading, **no credentials are required at all** — those paths are fully offline / public-read.

## Documentation

- [`docs/tool-reference.md`](./docs/tool-reference.md) — the full MCP tool, resource, and prompt catalog.
- [`docs/client-setup.md`](./docs/client-setup.md) — per-client setup (Claude Code, Cursor, Codex) + verifying the connection.
- [`docs/workflows.md`](./docs/workflows.md) — end-to-end recipes (research → author → backtest → paper).
- [`docs/strategy-spec.md`](./docs/strategy-spec.md) — the declarative strategy spec, indicators, and rule whitelist.
- [`docs/safety.md`](./docs/safety.md) — the safety model: dry-run defaults, risk limits, arming, the kill switch, key scoping.
- [`docs/troubleshooting.md`](./docs/troubleshooting.md) — common issues and fixes.
- [`PRD.md`](./PRD.md) — the authoritative spec and 8-phase build plan.

## Development

This repo uses Python 3.12 managed with `uv`.

```bash
uv sync                      # install deps into .venv
uv run trader-mcp            # run the MCP server over stdio
uvx --from . trader-mcp      # run as an installed entry point

uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyright               # type-check (or: uv run mypy src)

uv run pytest                # full test suite
uv run pytest path/to/test_file.py::test_name   # a single test
uv run pytest --cov          # with coverage

uv run pre-commit install    # enable lint/format on commit
```

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full developer guide, architecture invariants, and lane discipline.

## Safety & disclaimers

`trader-mcp` is **safe-by-default**: strategy specs are data (never executable code), order placement is **dry-run / paper** unless explicitly gated, and **real-money live trading is gated behind explicit arming + per-strategy risk limits and has not been opened** (no `live` session mode exists). See [`docs/safety.md`](./docs/safety.md).

**Not financial advice.** This software is provided "as is", without warranty of any kind. Trading cryptocurrencies carries substantial risk of loss. You assume all responsibility and risk for any use of this software, including any orders it places. The authors are not liable for any losses. Nothing here is a recommendation to buy or sell any asset.

## License

[MIT](./LICENSE) © 2026 Simon Lacey
