# trader-mcp — Product Requirements Document

> A terminal-first, AI-native crypto trading platform delivered as a **Model Context Protocol (MCP) server**.
> Connect your AI assistant (Claude Code, Codex, Cursor) over MCP, **prompt your way to a strategy**, **backtest it on real market data**, and **deploy to Coinbase, Kraken, Gemini, or Crypto.com** — all from your editor or chat.

- **Status:** Draft v1 — approved plan, pre-implementation
- **Last updated:** 2026-05-30
- **Owner:** simon@dashboardhosting.com
- **Repo:** `trader-mcp`

---

## 1. Vision & Positioning

Trading tooling today is either (a) heavyweight GUIs/platforms or (b) raw exchange SDKs that require you to write everything yourself. `trader-mcp` is for **developers and "vibe traders"** who live in a terminal/editor and want to drive the entire strategy lifecycle — research → author → backtest → paper → live — through natural-language prompts to an AI assistant, with deterministic, typed tools doing the real work underneath.

**One-liner:** *Prompt a strategy, backtest it on real data, deploy it to your exchange — without leaving your editor.*

### Why now
- The **MCP ecosystem is mature and stable** (`@modelcontextprotocol` SDKs, stdio transport used by Claude Code/Cursor/Codex, typed structured outputs).
- **CCXT unifies all four target exchanges** behind one interface for both REST and WebSocket (CCXT Pro), spot and perpetual swaps — so a single integration layer covers Coinbase, Kraken, Gemini, and Crypto.com, all legally accessible to US persons.
- Exchanges are **actively courting AI traders** (e.g. Coinbase's and Kraken's developer platforms and AI-agent toolkits), validating the "vibe trader" audience.
- No existing MCP server combines **multi-exchange execution + real-data backtesting + prompt-driven strategy authoring** — clear whitespace.

---

## 2. Target Users

| Persona | Needs |
|---|---|
| **Vibe trader** | Describe an idea in plain English; get a backtested, ready-to-paper-trade strategy with clear metrics. |
| **Developer / quant-curious** | Programmatic, scriptable, version-controllable strategies; reproducible backtests; clean APIs. |
| **AI agent** | Typed tools with structured outputs, deterministic behavior, safe-by-default execution, good error messages. |

---

## 3. Goals & Non-Goals

### Goals (v1)
- One MCP server, installable via `uvx trader-mcp`, that works with Claude Code, Cursor, and Codex.
- Unified market data + history across Coinbase/Kraken/Gemini/Crypto.com via CCXT.
- A **declarative strategy spec** + template library that an AI can author and validate.
- A backtesting engine that runs the *same* strategy interpreter used for live, producing trustworthy metrics + tear sheets on **real cached data**.
- Paper trading and exchange **testnet** execution.
- A safety layer (key scoping, dry-run, risk limits, idempotency) ready for the later live-trading phase.

### Non-Goals (v1)
- Real-money live trading (lands in Phase 6, behind explicit guardrails).
- Arbitrary AI-generated strategy *code* execution (declarative spec only in v1; sandboxed code is a future option).
- A hosted/remote multi-tenant service (local stdio first; remote HTTP is a future phase).
- A GUI/web dashboard, mobile app, or social/copy-trading features.
- Financial advice, signals-as-a-service, or any guarantee of profitability.

---

## 4. Key Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| **Runtime / distribution** | **Python 3.12 + `uv`, shipped via `uvx trader-mcp`** | Best-in-class backtesting ecosystem (vectorbt, backtesting.py, quantstats, indicators); CCXT's reference implementation; near-zero install. Server language is invisible to the AI client. |
| **Strategy representation** | **Declarative typed spec + template library** | Deterministic, safe (no arbitrary code execution), and guarantees **backtest↔live parity** via one shared interpreter. |
| **v1 safety posture** | **Backtest + paper + testnet only** | No real-money orders until a dedicated guardrailed phase. Fastest path to a safe, demoable product. |
| **Exchange rollout** | **Exchange-agnostic on CCXT; US-accessible venues only; validate Coinbase first** | All four available at the adapter level; deep-validate **Coinbase** (US-regulated, CCXT-Certified) first, then certify Kraken, Gemini, Crypto.com. US-legal perps via Coinbase Derivatives / Kraken Futures. |
| **Jurisdiction** | **US-accessible exchanges only (v1)** | Target venues must be legal for US persons; offshore exchanges (Bybit/BloFin/Toobit/WeeX) are explicitly out of scope for v1. US venues quote USD/USDC; perps limited to CFTC-regulated products. |

---

## 5. Technical Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  AI client: Claude Code / Cursor / Codex                      │
└───────────────┬───────────────────────────────────────────────┘
                │  MCP over stdio  (remote HTTP transport: future)
                ▼
┌─────────────────────────────────────────────────────────────┐
│  trader-mcp server (Python, official MCP SDK / FastMCP)        │
│                                                               │
│  Tool surface (typed inputs + structured outputs):            │
│   • Market data & discovery   • Strategy author/validate       │
│   • Historical data sync      • Backtest + optimize            │
│   • Paper / testnet execution • Portfolio / positions / risk   │
│                                                               │
│  ┌──────────────┐  ┌────────────────┐  ┌──────────────────┐   │
│  │ Exchange      │  │ Strategy        │  │ Backtest /        │   │
│  │ adapter       │  │ spec + engine   │  │ live runtime      │   │
│  │ (CCXT + Pro)  │  │ (shared interp) │  │ (same interp)     │   │
│  └──────┬───────┘  └────────┬───────┘  └────────┬─────────┘   │
│         │                   │                    │             │
│  ┌──────▼───────┐  ┌────────▼─────────┐  ┌───────▼─────────┐   │
│  │ Data cache    │  │ Indicators       │  │ Safety layer     │   │
│  │ DuckDB/Parquet│  │ (pandas-ta/TA)   │  │ keys·dry-run·risk│   │
│  └──────────────┘  └──────────────────┘  └──────────────────┘   │
└───────────────┬───────────────────────────────────────────────┘
                │
                ▼
   Coinbase · Kraken · Gemini · Crypto.com   (REST + WebSocket via CCXT)
```

### 5.1 Core stack
- **Language/tooling:** Python 3.12, [`uv`](https://github.com/astral-sh/uv) for envs & distribution, `ruff` (lint+format), `pyright`/`mypy` (types), `pytest` (+`pytest-asyncio`).
- **MCP:** official Python SDK (`mcp`, FastMCP high-level API). stdio transport for v1; Pydantic models for tool I/O and `outputSchema`/structured content.
- **Exchange connectivity:** `ccxt` (async) + CCXT Pro WebSockets (now part of `ccxt`). Single unified adapter for all four exchanges.
- **Backtesting:** custom **event-driven spec interpreter** as the source of truth (ensures live parity), cross-validated against `backtesting.py`; `vectorbt` / `Optuna` for fast parameter sweeps & walk-forward; `quantstats` for tear sheets.
- **Indicators:** `pandas-ta` (or `ta`) as default pure-Python; optional TA-Lib (C) for speed.
- **Data store:** DuckDB + Parquet for cached OHLCV (columnar, fast, file-based, no server).
- **Config/secrets:** env vars / `.env` / OS keyring; never logged; per-exchange key isolation and read-vs-trade scoping.
- **Validation:** Pydantic v2 everywhere (strategy spec, tool args, results).

### 5.2 MCP tool surface (v1 target)
Grouped; names indicative. All return structured, typed outputs.

**Connectivity / admin**
- `list_exchanges`, `get_exchange_capabilities`, `health_check`, `get_server_status`

**Market data & discovery**
- `search_symbols`, `list_markets`, `get_ticker`, `get_ohlcv`, `get_order_book`, `get_recent_trades`, `get_funding_rate` (perps)

**Historical data management**
- `sync_history` (paginated OHLCV download → cache), `list_cached_datasets`, `inspect_dataset` (coverage/gaps)

**Strategy authoring**
- `list_strategy_templates`, `create_strategy`, `validate_strategy`, `get_strategy`, `list_strategies`, `update_strategy`, `delete_strategy`

**Backtesting & optimization**
- `run_backtest`, `get_backtest_report`, `compare_backtests`, `optimize_strategy` (param sweep / walk-forward)

**Paper / testnet execution**
- `start_session` (paper|testnet), `place_order`, `cancel_order`, `get_open_orders`, `get_positions`, `get_balance`, `deploy_strategy`, `stop_strategy`, `get_session_status`

**Portfolio / analytics**
- `get_portfolio`, `get_pnl`, `get_trade_history`

**Safety (wired in v1, enforced for live in Phase 6)**
- `set_risk_limits`, `arm_live_trading`, `kill_switch`

**MCP resources & prompts**
- Resources: cached datasets, saved strategy specs, backtest reports.
- Prompts: guided "design a strategy", "explain this backtest", "diagnose drawdown".

### 5.3 Declarative strategy spec (concept)
A versioned Pydantic schema, e.g.:
```yaml
name: rsi-mean-reversion-btc
exchange: coinbase
symbol: BTC/USD            # spot (US venues quote USD/USDC, not USDT)
timeframe: 1h
indicators:
  - {id: rsi, kind: rsi, length: 14}
entry:
  long:  "rsi < 30"
  short: "rsi > 70"
exit:
  long:  "rsi > 50"
  short: "rsi < 50"
position_sizing: {mode: percent_equity, value: 5}
risk: {stop_loss_pct: 2, take_profit_pct: 4, max_leverage: 3}   # max_leverage applies to US-legal perps (e.g. exchange: kraken, symbol: BTC/USD:USD on Kraken Futures)
fees: {taker: 0.00055, maker: 0.0002}
```
- Rule expressions are parsed into a **safe, sandboxed expression evaluator** (whitelisted indicators/operators — no arbitrary Python).
- The **same interpreter** consumes historical bars (backtest) or streaming bars (paper/live) → identical behavior.

### 5.4 Safety model (cross-cutting)
- **Key scoping:** separate read-only vs trade-enabled keys; validate scope on connect; refuse trade ops with read-only keys.
- **Jurisdiction / geo-eligibility:** v1 targets only exchanges legally available to US persons (Coinbase, Kraken, Gemini, Crypto.com). Validate that the configured exchange **and market type** are permitted before placing/arming orders; surface a jurisdiction disclaimer. US perps are limited to CFTC-regulated products (Coinbase Derivatives, Kraken Futures); spot quotes are USD/USDC.
- **Dry-run default:** `place_order`/`deploy_strategy` simulate unless explicitly armed.
- **Arming:** real-money live requires `arm_live_trading` + per-strategy risk limits set.
- **Risk limits engine:** max position size, max daily loss, leverage caps, max order notional, max open positions.
- **Idempotency:** client-generated order IDs to prevent dup orders on retries.
- **Kill switch:** cancel-all + halt one exchange or everything.
- **Audit log:** every order intent/result logged (secrets redacted).
- **Disclaimers:** not financial advice; user assumes all risk; surfaced in docs and key tool responses.

---

## 6. Phased Delivery Plan (with checklists)

> Convention: each phase ends with an **Exit criteria** gate. Coinbase is the reference exchange validated first in every relevant phase.

### Phase 0 — Foundations & scaffolding
**Goal:** A runnable, well-engineered MCP skeleton with one trivial tool.
- [x] Initialize repo: `pyproject.toml` (PEP 621), `uv` workspace, `src/trader_mcp/` layout
- [x] Tooling: `ruff` (lint+format), `pyright`/`mypy`, `pre-commit` hooks
- [x] Test harness: `pytest`, `pytest-asyncio`, coverage config
- [x] CI: GitHub Actions (lint, type-check, tests on 3.11/3.12)
- [x] MCP server skeleton (FastMCP) over **stdio**; entry point `trader-mcp`
- [x] `health_check` + `get_server_status` tools (typed/structured output)
- [x] Structured logging with secret redaction; log levels via env
- [x] Config/secrets loader (env → `.env` → keyring); error taxonomy & error-to-MCP mapping
- [x] `CONTRIBUTING.md`, base `README.md`, license, `.gitignore`, `.env.example`, `.mcp.json` example
- **Exit:** `uvx --from . trader-mcp` boots; client lists tools; `health_check` returns OK; CI green.

### Phase 1 — Exchange connectivity & market data (CCXT)
**Goal:** Read live market data from all four exchanges via one adapter; Coinbase validated.
- [x] CCXT async adapter + exchange registry (`coinbase`, `kraken`, `gemini`, `cryptocom`)
- [x] Per-exchange capability map (spot/swap, timeframes, ws support)
- [x] Symbol/market normalization (unified `BASE/QUOTE:SETTLE` notation)
- [x] Tools: `list_exchanges`, `get_exchange_capabilities`, `search_symbols`, `list_markets`
- [x] Tools: `get_ticker`, `get_ohlcv`, `get_order_book`, `get_recent_trades`, `get_funding_rate`
- [x] Rate-limit handling, retries/backoff, timeout & error mapping
- [x] Read-only API key validation + scope detection
- [x] Testnet/sandbox endpoint wiring (where supported)
- [ ] Deep-validate **Coinbase**; smoke-test Kraken/Gemini/Crypto.com; log known quirks _(code follow-on **done**: registry ids, reference, default exchange, CLI, live suite, and CI probe are all re-pointed offshore→US, with Coinbase the reference and USD quoting; only the networked run remains — deferred to a US-eligible, non-sandboxed host, as this build environment has no exchange network. Owner: exchange-adapter-engineer.)_
- **Exit:** All market-data tools return correct, normalized data on Coinbase; basic parity smoke test on Kraken/Gemini/Crypto.com.

### Phase 2 — Historical data pipeline & cache
**Goal:** Reliable, resumable local history for real-data backtests.
- [ ] Paginated `fetchOHLCV` downloader (handles per-exchange page limits)
- [ ] DuckDB + Parquet store; schema (exchange, symbol, timeframe, ts, OHLCV)
- [ ] `sync_history` (incremental, resumable); `list_cached_datasets`; `inspect_dataset`
- [ ] Data-quality checks: gap detection, dedupe, monotonic timestamps, timezone normalization
- [ ] Multi-timeframe support + resampling
- [ ] Expose cached datasets as MCP **resources**
- **Exit:** Can sync ≥1 year of BTC/USD 1h on Coinbase, detect/repair gaps, and serve it to the backtester.

### Phase 3 — Strategy spec & template library
**Goal:** An AI can author and validate a strategy via prompts.
- [ ] Pydantic strategy schema (indicators, entry/exit rules, sizing, risk, fees) with versioning
- [ ] Safe expression evaluator (whitelisted functions/operators; no arbitrary code)
- [ ] Indicator library wrapper (pandas-ta/`ta`; optional TA-Lib)
- [ ] Template library: MA cross, RSI mean-reversion, breakout (Donchian), MACD, Bollinger, grid, DCA
- [ ] Tools: `list_strategy_templates`, `create_strategy`, `validate_strategy`, `get/list/update/delete_strategy`
- [ ] Strategy persistence (file-based, version-controllable); expose as MCP resources
- [ ] MCP **prompts** for guided strategy design
- [ ] Rich validation errors (AI-friendly: what's wrong + how to fix)
- **Exit:** From a plain-English prompt, the AI produces a valid, persisted strategy spec using a template.

### Phase 4 — Backtesting engine & analytics
**Goal:** Trustworthy backtests on real data with clear, structured reports.
- [ ] Event-driven interpreter executing the spec bar-by-bar (the shared backtest↔live code path)
- [ ] Realistic fills: taker/maker fees, configurable slippage, **funding for perps**, leverage/margin
- [ ] Metrics: total/annualized return (CAGR), Sharpe, Sortino, max drawdown, win rate, profit factor, exposure, trade count
- [ ] `quantstats` tear sheet generation; report as structured output + saved resource
- [ ] Tools: `run_backtest`, `get_backtest_report`, `compare_backtests`
- [ ] `optimize_strategy`: parameter sweep + **walk-forward** (vectorbt/Optuna); look-ahead/leakage guards
- [ ] Cross-validate engine vs `backtesting.py` on reference strategies (parity test)
- [ ] Deterministic/reproducible runs (seeded, pinned data snapshot)
- **Exit:** Backtest a template strategy on cached Coinbase data; metrics match the cross-validation engine within tolerance; report returned to the AI.

### Phase 5 — Paper trading & testnet execution
**Goal:** Run a strategy live against simulated and testnet venues using the same interpreter.
- [ ] Live data feed via CCXT Pro WebSockets (candles/trades/order book)
- [ ] Strategy runtime consuming streaming bars through the **same interpreter** as backtest
- [ ] Paper broker: simulated fills, balances, positions, PnL
- [ ] Exchange **testnet** execution (Coinbase sandbox / Kraken demo where available)
- [ ] Tools: `start_session` (paper|testnet), `place_order`, `cancel_order`, `get_open_orders`, `get_positions`, `get_balance`
- [ ] Tools: `deploy_strategy`, `stop_strategy`, `get_session_status`
- [ ] Order/position reconciliation; idempotent client order IDs
- [ ] Portfolio/analytics tools: `get_portfolio`, `get_pnl`, `get_trade_history`
- **Exit:** Deploy a strategy in paper mode and on Coinbase sandbox; orders/positions/PnL reconcile correctly.

### Phase 6 — Live trading guardrails (real money — gated)
**Goal:** Enable real-money execution safely, exchange by exchange.
- [ ] Risk-limits engine: max position, max daily loss, leverage cap, max order notional, max open positions
- [ ] `set_risk_limits`, `arm_live_trading` (explicit, expiring arm), confirmation gates
- [ ] Dry-run default for all live order paths; explicit opt-in required
- [ ] `kill_switch` (cancel-all + halt per-exchange/global); circuit breakers
- [ ] Full audit log of order intents/results (redacted); optional notifications/alerts (webhook/desktop)
- [ ] Live certification per exchange: **Coinbase → Kraken → Gemini → Crypto.com** (min-size real-order tests, quirk handling); US-legal perps (Coinbase Derivatives / Kraken Futures) certified separately under CFTC rules
- [ ] Security review of secret handling & key scoping
- **Exit:** A min-size real order places & cancels correctly on Coinbase with all guardrails enforced; runbook documented.

### Phase 7 — Hardening, docs & distribution
**Goal:** Ship a polished, documented, installable product.
- [ ] Test coverage: unit + recorded-fixture integration (VCR-style) + testnet integration suite
- [ ] Resilience: reconnect logic, partial-fill handling, clock-skew, exchange downtime
- [ ] Docs: README, tool reference, **strategy spec reference**, safety guide, per-client setup (Claude Code/Cursor/Codex), disclaimers
- [ ] Example end-to-end workflows ("prompt → backtest → paper → testnet")
- [ ] Publish to PyPI; verify `uvx trader-mcp`; semantic versioning + `CHANGELOG.md`
- [ ] `.mcp.json` / client config snippets; troubleshooting guide
- **Exit:** A new user installs via `uvx trader-mcp`, connects their AI client, and completes the example workflow from docs alone.

### Phase 8 — Future / optional
- [ ] Remote **Streamable HTTP** transport + auth for multi-user/hosted deployment
- [ ] Opt-in **sandboxed code-strategies** (beyond declarative spec)
- [ ] Additional exchanges (CCXT makes this incremental) — incl. offshore venues (Bybit/BloFin/Toobit/WeeX) for non-US users, gated by jurisdiction
- [ ] Advanced analytics (Monte Carlo, regime detection), strategy portfolios, scheduling/cron deploys
- [ ] Optional lightweight web dashboard for monitoring

---

## 7. Success Metrics
- **Activation:** time from `uvx trader-mcp` to first successful backtest < 10 minutes.
- **Reliability:** market-data + backtest tools >99% success on Coinbase; backtest results reproducible run-to-run.
- **Parity:** paper/live behavior matches backtest interpreter on identical inputs (automated parity tests pass).
- **Safety:** zero unintended real-money orders; 100% of live order paths gated by arming + risk limits.
- **Breadth:** all four exchanges pass the market-data + paper certification suite.

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Gemini/Crypto.com less algo-battle-tested in CCXT than Coinbase/Kraken | Bugs/edge cases in data or orders | Validate Coinbase first; per-exchange certification suite; quirk registry; graceful capability degradation |
| Backtest overfitting / look-ahead bias | Misleading results, user losses | Walk-forward, out-of-sample, leakage guards, prominent caveats in reports |
| Backtest↔live divergence | Live behaves unlike tests | Single shared interpreter; automated parity tests |
| Secret/key leakage | Account compromise | Keyring/env only, never logged, scope detection, security review (Phase 6) |
| Real-money mistakes | Financial loss | Backtest/paper/testnet-first; dry-run default; arming; risk limits; kill switch; idempotency |
| Exchange rate limits / downtime | Failures, partial state | Backoff, reconnects, reconciliation, circuit breakers |
| Regulatory/legal (incl. US jurisdiction) | Liability; trading on a non-permitted venue | US-accessible exchanges only (v1); geo-eligibility checks; US perps limited to CFTC-regulated products; clear "not financial advice" disclaimers; user assumes all risk; no custody of funds |
| MCP SDK v2 churn (stable Q1 2026) | Rework | Pin v1.x (production-recommended); isolate SDK behind an internal interface |

---

## 9. Open Questions (revisit during build)
- Indicator backend default: `pandas-ta` vs `ta` vs bundling TA-Lib (install friction trade-off).
- Strategy storage location convention (project-local `./strategies` vs user config dir).
- Notification channels for Phase 6 (webhook, desktop, email?).
- Whether to expose CCXT-Pro live streams as MCP resources/subscriptions in v1 or defer to Phase 5.
- US perpetual-futures support — in scope via CFTC-regulated Coinbase Derivatives / Kraken Futures (2026 rule change); confirm exact CCXT coverage and unified symbols per venue.
- Per-US-state restrictions (some tokens / leverage unavailable in NY, WA, etc.) — detect and surface to the user.

---

## 10. Appendix — Confirmed exchange/CCXT support
From the live CCXT exchange registry (US-accessible venues only):

| Exchange | CCXT id(s) | US markets | US status / regulator | US-legal perps | Reliability tier | WebSocket (CCXT Pro) |
|---|---|---|---|---|---|---|
| Coinbase | `coinbase` | Spot (USD/USDC) | US-regulated (SEC/CFTC) | Coinbase Derivatives (CFTC) — emerging | **CCXT Certified** (reference) | ✅ |
| Kraken | `kraken`, `krakenfutures` | Spot (USD/USDC); futures | US-regulated; CFTC futures | Kraken Futures (`krakenfutures`) | **CCXT Certified** | ✅ |
| Gemini | `gemini` | Spot (USD/USDC) | US-regulated (NYDFS) | None (US) | Supported | ✅ |
| Crypto.com | `cryptocom` | Spot (USD/USDC) | US MSB (app) | Restricted for US | Supported | ✅ |

All four are US-accessible, CCXT-supported centralized exchanges offering unified OHLCV history, `createOrder` execution, and WebSocket streaming through one CCXT integration. US venues quote in USD/USDC (not USDT); US-legal perpetual/futures trading is limited to CFTC-regulated products (Coinbase Derivatives, Kraken Futures). Pin exact CCXT ids (e.g. `coinbase` vs `coinbaseexchange`/`coinbaseadvanced`, and `krakenfutures`) against the installed CCXT version at implementation.
