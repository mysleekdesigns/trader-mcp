# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status: pre-implementation

This repo is **greenfield**. There is no Python source, `pyproject.toml`, or test suite yet — only `PRD.md`, config, and research artifacts. **`PRD.md` is the source of truth**: it defines the locked architecture, the tool surface, the strategy spec, the safety model, and an 8-phase build plan with per-phase exit criteria. Read it before writing code. The next concrete work is **Phase 0 (Foundations & scaffolding)** — see PRD §6.

When you scaffold or extend the project, keep it consistent with the locked decisions below rather than re-litigating them.

## What trader-mcp is

A terminal-first, AI-native crypto trading platform shipped as a **Model Context Protocol (MCP) server**. An AI client (Claude Code / Cursor / Codex) connects over MCP stdio and drives the full strategy lifecycle by prompt: research market data → author a declarative strategy → backtest on real cached data → paper/testnet trade → (later, gated) live trade. Target exchanges are **Bybit, BloFin, Toobit, WeeX**, all reached through one CCXT adapter. Bybit is the reference exchange, validated first in every phase.

## Locked technical decisions (do not re-litigate — PRD §4, §5)

- **Runtime:** Python 3.12, managed with [`uv`](https://github.com/astral-sh/uv), distributed via `uvx trader-mcp`. Package layout `src/trader_mcp/`.
- **MCP:** official Python SDK (`mcp`) with the FastMCP high-level API; **stdio** transport for v1 (remote Streamable HTTP is a future phase). Pin MCP SDK to v1.x and isolate it behind an internal interface (SDK v2 churn risk).
- **Exchanges:** `ccxt` (async) + CCXT Pro WebSockets — a single unified adapter for all four exchanges.
- **Strategy representation:** a **declarative, typed Pydantic v2 spec** + template library. **No arbitrary AI-generated code execution in v1** — rule expressions run through a safe, sandboxed evaluator with whitelisted indicators/operators only.
- **Backtesting:** a custom **event-driven spec interpreter** is the source of truth, cross-validated against `backtesting.py`; `vectorbt`/`Optuna` for sweeps & walk-forward; `quantstats` for tear sheets.
- **Indicators:** `pandas-ta` (or `ta`) by default; optional TA-Lib for speed.
- **Data store:** DuckDB + Parquet for cached OHLCV (file-based, no server).
- **Tooling:** `ruff` (lint+format), `pyright`/`mypy` (types), `pytest` + `pytest-asyncio`, `pre-commit`. Validation with Pydantic v2 everywhere (tool args, strategy spec, results).

## Two architectural invariants that drive everything

1. **One interpreter, used for both backtest and live.** The same event-driven spec interpreter consumes historical bars (backtest) and streaming bars (paper/live). This guarantees backtest↔live parity. **Never fork strategy-execution logic between the backtest and live paths** — any divergence is a bug. Automated parity tests are a first-class requirement (PRD §5.3, §7).
2. **Safe-by-default execution.** Strategy specs are data, never code. `place_order`/`deploy_strategy` are dry-run unless explicitly armed. Real-money live trading is gated behind `arm_live_trading` + per-strategy risk limits and does not land until Phase 6. Keys are scoped read-only vs trade-enabled; secrets come from env/`.env`/keyring and are never logged. Treat these guardrails as non-negotiable invariants when adding any execution path.

## MCP tool surface (PRD §5.2)

Tools are grouped: connectivity/admin, market data & discovery, historical data sync, strategy authoring, backtest & optimize, paper/testnet execution, portfolio/analytics, and safety (`set_risk_limits`, `arm_live_trading`, `kill_switch`). All tools take typed inputs and return structured/typed outputs (Pydantic models → `outputSchema`). Cached datasets, saved strategy specs, and backtest reports are also exposed as MCP **resources**; guided design flows as MCP **prompts**.

## Intended commands (once Phase 0 scaffolding exists)

These don't exist yet — establish them in Phase 0, then keep this section accurate.

```bash
uv sync                      # install deps into .venv
uv run trader-mcp            # run the MCP server over stdio
uvx --from . trader-mcp      # run as an installed entry point (Phase 0 exit check)

uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyright               # type-check (or: uv run mypy src)

uv run pytest                # full test suite
uv run pytest path/to/test_file.py::test_name   # a single test
```

Keep `uv.lock` and `.python-version` tracked; everything in `.gitignore` (caches, `.env`, `*.duckdb`, `*.parquet`, `backtests/`, `reports/`, `logs/`) stays out of git.

## The agent team, skills & hooks (`.claude/`)

This repo ships a **specialized sub-agent team** plus **skills** and **hooks** that map to the PRD's architecture. They live under `.claude/` and are checked into version control so the whole team shares them.

### Sub-agents (`.claude/agents/`)

Each agent owns one domain and carries the locked decisions + invariants for it in its system prompt. The main (orchestrator) session delegates by matching a task to an agent's `description`; you can also name an agent explicitly (`@exchange-adapter-engineer …`).

| Agent | Owns | Use it for |
| --- | --- | --- |
| `scaffolding-engineer` | repo skeleton, `uv`, `pyproject`, toolchain | Phase 0 foundations, packaging, entry point, ruff/pyright/pytest/pre-commit |
| `mcp-server-engineer` | `src/trader_mcp/server`, tool registry | FastMCP tools/resources/prompts, stdio, typed I/O + `outputSchema`, SDK isolation |
| `exchange-adapter-engineer` | `src/trader_mcp/exchanges` | the single CCXT (+Pro WS) adapter, Bybit/BloFin/Toobit/WeeX, rate limits, key scoping |
| `data-pipeline-engineer` | `src/trader_mcp/data` | DuckDB+Parquet OHLCV store, historical sync, gap repair, dataset resources |
| `strategy-spec-engineer` | `src/trader_mcp/strategy`, indicators | Pydantic spec, templates, safe sandboxed evaluator + whitelist, pandas-ta |
| `backtest-engine-engineer` | `src/trader_mcp/engine` | the one event-driven interpreter (backtest **and** live), sweeps, parity, tear sheets |
| `risk-safety-engineer` | `src/trader_mcp/safety` | dry-run defaults, `arm_live_trading`, risk limits, `kill_switch`, secrets — **must gate every execution path** |
| `qa-parity-engineer` | `tests/`, parity harness | pytest/pytest-asyncio, the backtest↔live parity suite, coverage |
| `quant-research-analyst` | `notes/`/`research/` (read-only on `src/`) | crawlforge web research on exchange/library APIs, indicators, strategy ideas |
| `code-reviewer` | review only (no edits) | invariant-focused review of a diff before it lands |

### Launching the team in parallel

Sub-agents run in their **own context** and **report back to the main session only** (they don't talk to each other) — so the main session is the orchestrator that splits work, integrates results, and resolves cross-agent contracts.

- **To parallelize, dispatch several agents in a single message** (multiple Agent/Task calls at once). Each spawn runs concurrently in the background.
- **Give each a non-overlapping slice** aligned to its ownership column above, so they don't edit the same files.
- **When agents must edit overlapping areas concurrently, run them with worktree isolation** (`isolation: "worktree"`) so their changes don't collide; otherwise serialize edits to shared files.
- **Respect the dependency order:** `scaffolding-engineer` goes first (everything depends on the layout). Then fan out `mcp-server`, `exchange-adapter`, `data-pipeline`, and `strategy-spec` engineers in parallel. `backtest-engine` depends on strategy-spec + data. `risk-safety` and `qa-parity` are cross-cutting; `code-reviewer` gates the merge.

Example parallel fan-out (after Phase 0): dispatch `exchange-adapter-engineer` (Bybit OHLCV fetch), `data-pipeline-engineer` (DuckDB schema + sync), and `strategy-spec-engineer` (spec models + 3 templates) in one message; once they return, hand the contracts to `mcp-server-engineer` to expose as tools and to `qa-parity-engineer` for tests.

### Skills (`.claude/skills/`)

Procedures Claude loads on demand (or you invoke with `/name`). Keep them current as the implementation lands.

| Skill | Invoke | Purpose |
| --- | --- | --- |
| `/add-mcp-tool` | you or Claude | checklist to add an MCP tool the safe, typed way (preloaded into `mcp-server-engineer`) |
| `/author-strategy` | you or Claude | guided declarative-strategy authoring within the whitelist (preloaded into `strategy-spec-engineer`) |
| `/run-backtest` | you or Claude | run interpreter → cross-validate vs backtesting.py → parity check → quantstats tear sheet (preloaded into `backtest-engine-engineer`) |
| `/safety-preflight` | you or Claude | safety checklist before any execution/arming/secret change (preloaded into `risk-safety-engineer`) |
| `/phase-gate` | **you only** | verify a PRD §6 phase's exit criteria (runs ruff/types/pytest) before advancing |
| `/next-phase` | you or Claude | orchestrate the next PRD §6 phase: fan out the agent team in parallel → integrate → phase-gate → code-review → check off PRD.md → commit & push (Phase 6+ live trading pauses for human authorization) |

### Hooks (`.claude/settings.json` → `.claude/hooks/`)

Deterministic guardrails that enforce the safety model regardless of model behavior (portable Python 3, stdlib-only):

- **`guard-bash.py`** (PreToolUse/Bash) — denies commands that print/read/stage secrets (`cat .env`, `printenv`, `git add .env`, reading `*.pem`/`*.key`). Template files like `.env.example` are allowed.
- **`guard-files.py`** (PreToolUse/file tools) — denies reading secret files; asks for confirmation before writing a secret file or editing the locked `PRD.md`.
- **`format-python.py`** (PostToolUse/edits) — runs `ruff format` + `ruff check --fix` on edited `.py` files (no-ops until Phase 0 toolchain exists).
- `permissions.deny` also blocks `Read` of `.env*`, `*.pem`, `*.key`, `id_rsa` as defense in depth.

> `.claude/settings.local.json` (not committed) enables the crawlforge MCP server. A team-wide allowlist of safe dev commands (`uv run`, `ruff`, `pytest`, read-only `git`) is intentionally **not** added here — run `/fewer-permission-prompts` if you want to opt into one.

## Operating rules for the team (read before delegating)

1. **`PRD.md` is the source of truth.** Do not re-litigate the locked decisions (§4, §5). The hook asks for confirmation before any PRD edit.
2. **Uphold the two invariants in every change:** one interpreter for backtest+live, and safe-by-default execution. `code-reviewer` and `risk-safety-engineer` treat violations as blockers.
3. **Stay in your lane.** Each agent edits its owned area; cross-area contracts are negotiated through the main session, not by reaching into another agent's modules.
4. **Bybit first.** Validate every phase on Bybit before fanning out to BloFin/Toobit/WeeX.
5. **Secrets are radioactive.** Never read, print, log, or commit `.env`/keys. The hooks enforce this; don't try to work around them.
6. **Gate execution.** Any change that places, arms, or routes orders, or touches credentials, must go through `risk-safety-engineer` and the `/safety-preflight` checklist. Live trading is Phase 6+.

## Repo layout notes

- `PRD.md` — the authoritative spec and phased plan. The most important file in the repo.
- `.mcp.json` + `.claude/settings.local.json` — wire up the **crawlforge** MCP server (web search/scrape) used for research during the build. This is tooling for *building* trader-mcp, not part of the product.
- `.claude/agents/` — the specialized sub-agent team (see "The agent team" above). `.claude/skills/` — reusable procedures (`/add-mcp-tool`, `/author-strategy`, `/run-backtest`, `/safety-preflight`, `/phase-gate`). `.claude/hooks/` + `.claude/settings.json` — the secret-protection and auto-format guardrails. All checked in and shared by the team.
- `cache/` — crawlforge's web-research cache (hash-named JSON). Gitignored; not product code. `jobs/`, `snapshots/`, `webhooks/` are empty placeholders.
- `.env` — local secrets, gitignored. Never read, print, or commit its contents.
