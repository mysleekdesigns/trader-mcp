# Changelog

All notable changes to **trader-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Phase 7 hardening, documentation, and distribution preparation (in progress).

## [0.1.0] - 2026-06-07

First usable pre-release. Market-data research, historical caching, declarative
strategy authoring, backtesting, and paper/testnet execution all work end to end.
**Live (real-money) trading is intentionally gated and deferred** — it requires
explicit arming plus per-strategy risk limits and has not been certified.

### Added
- **Foundations & scaffolding (Phase 0).** Python 3.12 + `uv` project skeleton
  under `src/trader_mcp/`, hatchling build backend, `trader-mcp` console entry
  point booting an MCP stdio server, and the full toolchain: ruff (lint+format),
  pyright/mypy, pytest + pytest-asyncio, pre-commit, and CI.
- **Exchange connectivity & market data (Phase 1).** A single CCXT (async) +
  CCXT Pro adapter behind a unified interface, plus market-data and discovery MCP
  tools (ticker, order book, OHLCV, markets/symbols listing, exchange status).
  Live-validation machinery (proxy support, opt-in live test suite,
  `trader-mcp-validate` CLI) for geo-blocked environments.
- **Historical data pipeline (Phase 2).** DuckDB + Parquet OHLCV cache with
  `sync_history`, `list_cached_datasets`, and `inspect_dataset` tools, gap repair,
  and cached datasets exposed as `dataset://` MCP resources.
- **Strategy authoring (Phase 3).** Declarative, typed Pydantic v2 strategy spec,
  a safe sandboxed rule evaluator with whitelisted indicators/operators (no
  arbitrary code execution), pandas-ta indicator integration, a template library,
  strategy-authoring tools, `strategy://` resources, and guided design prompts.
- **Backtesting engine & analytics (Phase 4).** One event-driven spec interpreter
  used for both backtest and live, a simulated broker with next-bar-open fills,
  native performance metrics, Optuna-driven optimization with walk-forward, a
  quantstats HTML tear sheet, backtest tools, and `backtest://` resources.
  Cross-validated against `backtesting.py`.
- **Paper & testnet execution (Phase 5).** A live `StrategyRuntime` driven by the
  same interpreter (full-prefix buffering for exact EWM parity), a paper broker,
  CCXT Pro WebSocket streams, idempotent client order IDs, and paper/testnet
  execution tools.
- **Risk & safety guardrails (Phase 6).** A risk engine, an expiring
  confirmation-gated `arm_live_trading`, a `kill_switch`, configurable risk limits,
  and a redacted audit log, all wired through a `SafetyController.preflight` step
  in the order path.
- **Distribution (Phase 7).** Semantic versioning, this `CHANGELOG.md`, and an
  MCP client config example (`.mcp.json.example`) for `uvx trader-mcp` across
  Claude Code, Cursor, and Codex.

### Changed
- Re-targeted the exchange set from offshore venues to US-regulated exchanges
  (Coinbase, Kraken, Gemini, Crypto.com); **Coinbase is the reference exchange**
  validated first in every phase. The default exchange is now `coinbase`.
- CI now probes the optional `vectorbt` dependency via `importlib.util.find_spec`
  so type-checking stays green when the `vbt` extra is not installed.

### Security
- **Safe-by-default execution.** Strategy specs are data, never code; rule
  expressions run only through the whitelisted sandboxed evaluator. Order
  placement and strategy deployment are dry-run unless explicitly armed.
- **Live trading is gated and deferred.** Real-money trading requires
  `arm_live_trading` plus per-strategy risk limits and is not yet certified;
  paper routes to simulation, testnet is gated, and unarmed live is denied.
- **Secrets are never logged.** Credentials are sourced from env / `.env` /
  keyring, scoped read-only vs trade-enabled, and redacted from the audit log.
  Build/runtime tooling and hooks block reading or committing secret files.

[Unreleased]: https://github.com/mysleekdesigns/trader-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mysleekdesigns/trader-mcp/releases/tag/v0.1.0
