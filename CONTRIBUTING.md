# Contributing to trader-mcp

Thanks for working on `trader-mcp`. This guide covers local setup, the commands you'll run, and the non-negotiable architecture rules. **[`PRD.md`](./PRD.md) is the source of truth** — read §4 and §5 before changing anything, and do not re-litigate the locked decisions.

## Prerequisites

- **Python 3.12** (the runtime/distribution target; pinned in `.python-version`).
- **[`uv`](https://github.com/astral-sh/uv)** for environment and dependency management.

## Setup

```bash
uv sync                   # create .venv and install deps + dev tools
uv run pre-commit install # enable ruff lint+format on commit
```

## Everyday commands

```bash
uv run trader-mcp            # run the MCP server over stdio
uvx --from . trader-mcp      # run as an installed entry point

uv run ruff check .          # lint
uv run ruff format .         # format (use --check in CI)
uv run pyright               # type-check (or: uv run mypy src)

uv run pytest                # full test suite
uv run pytest -q tests/test_server.py::test_health_check_returns_ok  # a single test
uv run pytest --cov          # with coverage
```

Before opening a PR, all of these must pass: `ruff check`, `ruff format --check`, `pyright` (or `mypy src`), and `pytest`. CI runs them on a **3.11 and 3.12** matrix.

## Project layout

```
src/trader_mcp/
  __init__.py        # package + __version__
  __main__.py        # python -m trader_mcp
  config.py          # typed settings & secret loading (env -> .env -> keyring)
  logging_config.py  # structured logging + secret redaction filter
  errors.py          # error taxonomy + error_to_mcp() mapping
  server/            # FastMCP app, tools, SDK isolation (_sdk.py)
  exchanges/         # CCXT adapter (Phase 1)
  data/              # DuckDB + Parquet OHLCV cache (Phase 2)
  strategy/          # declarative spec + safe evaluator (Phase 3)
  indicators/        # indicator wrapper (Phase 3)
  engine/            # the one event-driven interpreter (Phase 4)
  safety/            # dry-run, arming, risk limits, kill switch (Phase 6)
tests/               # pytest + pytest-asyncio
```

## Two architectural invariants (treated as blockers in review)

1. **One interpreter for backtest *and* live.** The same event-driven spec interpreter consumes historical bars (backtest) and streaming bars (paper/live). **Never fork strategy-execution logic** between the two paths — any divergence is a bug. Parity tests are a first-class requirement.
2. **Safe-by-default execution.** Strategy specs are *data*, never code. `place_order`/`deploy_strategy` are dry-run unless explicitly armed. Real-money live trading is gated behind `arm_live_trading` + per-strategy risk limits (Phase 6+). Keys are scoped read-only vs trade-enabled; **secrets come from env/`.env`/keyring and are never logged**.

## Conventions

- **Pydantic v2** for all tool inputs/outputs and the strategy spec.
- **MCP SDK is pinned to 1.x and isolated** behind `trader_mcp/server/_sdk.py` — that is the *only* module allowed to `import mcp`. Everything else imports from `trader_mcp.server`.
- **Async-first** (CCXT async + CCXT Pro). Tests use `pytest-asyncio` with `asyncio_mode = "auto"`, so `async def test_...` works without decorators.
- **Never read, print, log, or commit** `.env` or keys. Logging is redacted by `logging_config.RedactionFilter`; log to **stderr** (stdout is the MCP JSON-RPC channel).
- Lint/format/type rules live in `pyproject.toml`. The `.claude/` tooling directory is excluded from package linting.

## Lane discipline

Each area has an owner (see the agent table in [`CLAUDE.md`](./CLAUDE.md)). Edit your owned modules; negotiate cross-area contracts through the orchestrator rather than reaching into another area's files. **Coinbase is validated first** in every phase before fanning out to Kraken/Gemini/Crypto.com.

## Commits & PRs

- Branch off `development` (or as directed); don't commit directly to `main`.
- Keep changes scoped to one phase/concern. Update `PRD.md` checkboxes and `CLAUDE.md`'s "Intended commands" when behavior changes.
- A green toolchain + passing tests are required to merge.
