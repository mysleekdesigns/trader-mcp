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
- [x] Paginated `fetchOHLCV` downloader (handles per-exchange page limits)
- [x] DuckDB + Parquet store; schema (exchange, symbol, timeframe, ts, OHLCV)
- [x] `sync_history` (incremental, resumable); `list_cached_datasets`; `inspect_dataset`
- [x] Data-quality checks: gap detection, dedupe, monotonic timestamps, timezone normalization
- [x] Multi-timeframe support + resampling
- [x] Expose cached datasets as MCP **resources**
- **Exit:** Can sync ≥1 year of BTC/USD 1h on Coinbase, detect/repair gaps, and serve it to the backtester. _(Validated **offline**: a faithful paginating CCXT fake drives an 8760-bar BTC/USD 1h sync through the real adapter→store path, with gap inject/detect/repair and read-back as `OHLCVResult` to feed the backtester. The real **networked** Coinbase sync is deferred to a US-eligible, non-sandboxed host — this build env has no exchange network, same as the Phase 1 live box. Owner: data-pipeline-engineer.)_

### Phase 3 — Strategy spec & template library
**Goal:** An AI can author and validate a strategy via prompts.
- [x] Pydantic strategy schema (indicators, entry/exit rules, sizing, risk, fees) with versioning
- [x] Safe expression evaluator (whitelisted functions/operators; no arbitrary code)
- [x] Indicator library wrapper (pandas-ta/`ta`; optional TA-Lib) _(pandas-ta, Python ≥3.12; lazy import; 3.11 CI leg skips indicator-compute tests)_
- [x] Template library: MA cross, RSI mean-reversion, breakout (Donchian), MACD, Bollinger, grid, DCA
- [x] Tools: `list_strategy_templates`, `create_strategy`, `validate_strategy`, `get/list/update/delete_strategy` _(+ `list_indicators`; 8 strategy tools, 22 total)_
- [x] Strategy persistence (file-based, version-controllable); expose as MCP resources _(`strategy://catalog`, `strategy://{name}`)_
- [x] MCP **prompts** for guided strategy design _(`design_strategy`, `pick_template`, `explain_indicators`)_
- [x] Rich validation errors (AI-friendly: what's wrong + how to fix) _(`ValidationReport` issues carry `location` + `problem` + `fix`)_
- **Exit:** From a plain-English prompt, the AI produces a valid, persisted strategy spec using a template.

### Phase 4 — Backtesting engine & analytics
**Goal:** Trustworthy backtests on real data with clear, structured reports.
- [x] Event-driven interpreter executing the spec bar-by-bar (the shared backtest↔live code path) _(`engine/interpreter.py`: one `SpecInterpreter` with a single signal core; `signal_at(window)` derives from the same vectorized `signals(df)` — the live per-bar path == backtest path, proven 0-mismatch across all templates)_
- [x] Realistic fills: taker/maker fees, configurable slippage, **funding for perps**, leverage/margin _(`engine/broker.py`: pure in-memory `SimulatedBroker`, next-bar-open fills, intrabar SL/TP, flat-rate perp funding, leverage-scaled notional)_
- [x] Metrics: total/annualized return (CAGR), Sharpe, Sortino, max drawdown, win rate, profit factor, exposure, trade count _(`engine/metrics.py`, computed natively — no quantstats dependency for metrics)_
- [x] `quantstats` tear sheet generation; report as structured output + saved resource _(`engine/tearsheet.py` lazy/graceful HTML; reports persisted by `BacktestStore` and exposed as `backtest://catalog` + `backtest://{report_id}` resources)_
- [x] Tools: `run_backtest`, `get_backtest_report`, `compare_backtests` _(+ `optimize_strategy`, `generate_tearsheet`; 5 backtest tools, 27 total)_
- [x] `optimize_strategy`: parameter sweep + **walk-forward** (vectorbt/Optuna); look-ahead/leakage guards _(`engine/optimize.py`: native Optuna source-of-truth, vectorbt optional `vbt` extra; walk-forward with disjoint train/test, indicators recomputed per window)_
- [x] Cross-validate engine vs `backtesting.py` on reference strategies (parity test) _(`tests/test_crossvalidate_backtesting.py`: SMA-cross vs backtesting.py 0.6.5 matched on commission/next-open fills — final-equity drift 0.28%, total-return 0.70%, both ≤1%)_
- [x] Deterministic/reproducible runs (seeded, pinned data snapshot) _(stable `report_id` hash of spec+config+bars excluding wall-clock; seeded Optuna)_
- **Exit:** Backtest a template strategy on cached Coinbase data; metrics match the cross-validation engine within tolerance; report returned to the AI. _(Validated **offline**: backtests run through the real spec→interpreter→broker→metrics path on cached data via `OHLCVStore`, cross-validated against `backtesting.py` within tolerance, with the first-class backtest↔live parity suite green. A networked **Coinbase** sync to feed real cached bars is the only deferred step — same US-eligible, non-sandboxed host caveat as Phases 1–2. Owner: backtest-engine-engineer.)_

### Phase 5 — Paper trading & testnet execution
**Goal:** Run a strategy live against simulated and testnet venues using the same interpreter.
- [x] Live data feed via CCXT Pro WebSockets (candles/trades/order book) _(`ExchangeAdapter.watch_ohlcv`/`watch_trades`/`watch_order_book` async generators; bounded-backoff reconnection, clean cancellation; streamed bars normalized through the SAME helpers as REST so they feed the one interpreter identically; offline-validated with fakes — a real WS session is deferred to a US-eligible, non-sandboxed host, as this build environment has no exchange network)_
- [x] Strategy runtime consuming streaming bars through the **same interpreter** as backtest _(`execution/runtime.py`: `StrategyRuntime` drives every decision via `SpecInterpreter.signal_at` over an unbounded full-prefix buffer — reimplements NO signal logic; backtest↔live parity proven 0-mismatch across rule/grid/dca × slippage/funding/sizing/SL-TP, incl. EWM indicators RSI/EMA/MACD)_
- [x] Paper broker: simulated fills, balances, positions, PnL _(`execution/paper_broker.py`: online stateful pure-simulation broker, byte-identical fill math to the backtest `SimulatedBroker`; balances/positions/portfolio/PnL breakdown — realized/unrealized/fees/funding)_
- [x] Exchange **testnet** execution (Coinbase sandbox / Kraken demo where available) _(sandbox/testnet wiring via `set_sandbox_mode` through `ExchangeAdapter.create(testnet=...)`, refused (typed error) when unsupported; trade plumbing `create_order`/`cancel_order`/`fetch_order`/`fetch_open_orders`/`fetch_positions`/`fetch_balance` scoped trade-enabled-only; routing gated by `safety.evaluate_order`. Offline-validated with fakes; a real networked sandbox run is deferred to a US-eligible, non-sandboxed host. Owner: exchange-adapter-engineer)_
- [x] Tools: `start_session` (paper|testnet), `place_order`, `cancel_order`, `get_open_orders`, `get_positions`, `get_balance` _(safe-by-default: `place_order` consults `evaluate_order` before any order — paper simulates, testnet routes only with trade-enabled scope + US-eligible venue + dry-run off, live denied; idempotent client order IDs)_
- [x] Tools: `deploy_strategy`, `stop_strategy`, `get_session_status` _(deploy binds a `StrategyRuntime`+broker to a session and starts a cancellable run loop over the bar feed (paper: cached-bar replay; testnet: `watch_ohlcv`); 12 execution+portfolio tools, 27→39 total)_
- [x] Order/position reconciliation; idempotent client order IDs _(`get_open_orders`/`get_positions`/`get_balance` read from the live exchange (testnet) or the paper broker as the reconciliation surface; idempotency wired into `place_order` via `safety.make_client_order_id` (per-session monotonic seq → distinct deterministic COIDs) + `IdempotencyRegistry` retry dedupe. Deeper compare-and-repair reconciliation is a Phase 7 resilience item)_
- [x] Portfolio/analytics tools: `get_portfolio`, `get_pnl`, `get_trade_history` _(`server/portfolio.py`; typed structured outputs, internally consistent: equity == cash + position_value, total == realized + unrealized)_
- **Exit:** Deploy a strategy in paper mode and on Coinbase sandbox; orders/positions/PnL reconcile correctly. _(Paper mode validated end-to-end offline — deploy → run loop over cached bars → orders/positions/PnL reconcile, with backtest↔live parity proven 0-mismatch. The Coinbase **sandbox** half is code-complete and offline-validated with fakes; the networked sandbox run is deferred to a US-eligible, non-sandboxed host — same environment constraint as Phase 1.)_

### Phase 6 — Live trading guardrails (real money — gated)
**Goal:** Enable real-money execution safely, exchange by exchange.
- [x] Risk-limits engine: max position, max daily loss, leverage cap, max order notional, max open positions _(`safety/risk.py`: typed frozen `RiskLimits` + pure `check_order_risk(OrderRiskContext, RiskLimits) -> RiskCheck`; each `None` limit disabled, boundary `==` allowed / `>` breached, one violation string per breached cap. Wired into `place_order` via `SafetyController.preflight` (kill switch → risk → gate, fail-closed) ahead of any order. Notional caps require a known price: a limit order's price or a live position mark — a market order with no derivable price is DENIED while a notional cap is set rather than evaluated against a meaningless 0.0. Per-UTC-day realized-loss accounting is a noted follow-up (session-level magnitude used today). Owner: risk-safety-engineer)_
- [x] `set_risk_limits`, `arm_live_trading` (explicit, expiring arm), confirmation gates _(`safety/arming.py` + `server/safety.py`: `ArmingRegistry` is a confirmation-gated, TTL-expiring per-exchange/global arm (exact phrase `I UNDERSTAND THE RISKS`, ttl clamped to ≤3600s, wrong phrase/ttl≤0 raises a redacted `SafetyError`). MCP tools `set_risk_limits`, `arm_live_trading`, `disarm_live_trading`, `get_safety_status` are typed + structured-output. **The arm is built and unit-tested but is NOT yet consumed by any order path — opening the live wall is the deferred certification step below**; `evaluate_order`'s `live` branch still denies unconditionally even when armed. Owner: risk-safety-engineer)_
- [x] Dry-run default for all live order paths; explicit opt-in required _(safe-by-default holds: the process-wide `global_dry_run` default (on unless `TRADER_MCP_DRY_RUN` disables it) downgrades any routable decision to simulate, and the `live` branch is a hard deny regardless of arm/scope. The explicit, expiring opt-in machinery (`arm_live_trading`) exists; the real live-order route that would consume it is intentionally deferred to certification. `ServerSessionMode` stays `paper|testnet` — no MCP tool can open a live session.)_
- [x] `kill_switch` (cancel-all + halt per-exchange/global); circuit breakers _(`safety/kill_switch.py` + `kill_switch` MCP tool: global + per-exchange halt; `engage` best-effort cancels open paper orders across in-scope sessions (deployed + manual brokers) and blocks new orders via `preflight`; `reset` clears. The risk engine (max_daily_loss et al.) is the breaker that denies breaching orders. Auto-tripping circuit breakers (e.g. auto-engage the kill switch on a daily-loss breach) and testnet/live cancel-all are noted follow-ups for the live-certification step. Owner: risk-safety-engineer + mcp-server-engineer)_
- [x] Full audit log of order intents/results (redacted) _(`safety/audit.py`: append-only bounded `AuditLog` of `order_intent`/`order_result`/`denied`/`arm`/`disarm`/`kill_switch`/`set_risk_limits` events; every `detail` runs through `logging_config.redact` on record; `place_order` records intent + result, `preflight` records denies. Exposed via the `get_audit_log` tool. Optional notifications/alerts (webhook/desktop) are deferred to Phase 7 hardening.)_
- [ ] Live certification per exchange: **Coinbase → Kraken → Gemini → Crypto.com** (min-size real-order tests, quirk handling); US-legal perps (Coinbase Derivatives / Kraken Futures) certified separately under CFTC rules _(**DEFERRED — real money.** Requires a US-eligible, non-sandboxed host and explicit human authorization to open the live wall; not performed autonomously. The live wall (`evaluate_order` `live` → deny) stays up until this step.)_
- [x] Security review of secret handling & key scoping _(code-reviewer security pass: PASS on secret hygiene (no tool I/O, `ArmTicket`, `SafetyStatus`, audit `detail`, or error carries key material; redaction enforced on record; no `import mcp` outside `_sdk`), key scoping (read-only-vs-trade-enabled boundary unchanged; `require_trade_scope` still first on the route path; kill-switch cancel-all only cancels, no privilege escalation), and fail-closed (wrong phrase / ttl≤0 / unknown session / kill-switch engaged / risk breach / unpriceable-market-order-with-cap all deny or raise typed `SafetyError`). No blockers.)_
- **Exit:** A min-size real order places & cancels correctly on Coinbase with all guardrails enforced; runbook documented. _(**DEFERRED — real money.** Guardrail MACHINERY is complete and offline-validated (939 tests green, backtest↔live parity still 0-mismatch, the live wall intact); the real-order exit + runbook require human-authorized live certification on a networked host — see the deferred item above.)_

### Phase 7 — Hardening, docs & distribution
**Goal:** Ship a polished, documented, installable product.
- [x] Test coverage: unit + recorded-fixture integration (VCR-style) + testnet integration suite _(coverage pushed **91% → 95%** (`validate.py` 0%→85%, `strategy/evaluator.py` 92%→98%). VCR-style: a dependency-free in-repo cassette helper (`tests/integration/_cassettes.py`) replays SANITIZED recorded CCXT public payloads (`tests/fixtures/cassettes/<exchange>/`, Coinbase reference + Kraken) through the REAL `ExchangeAdapter` — an offline analogue of the live suite; an `assert_sanitized` guard fails on any key/secret/token in a fixture. Testnet suite: `tests/testnet/` gated by a new `testnet` marker (opt-in via `--testnet`/`TRADER_MCP_TESTNET_TESTS=1`, plus a second `TRADER_MCP_TESTNET_ALLOW_ORDERS=1` gate before any order scaffold); always-on safety-invariant tests in it re-prove the live wall denies even when armed; it NEVER runs in the default gate and NEVER places a live order. 1026 tests pass / 15 skipped offline. Owner: qa-parity-engineer)_
- [x] Resilience: reconnect logic, partial-fill handling, clock-skew, exchange downtime _(Reconnect: WS `_watch_loop` bounded-backoff reconnection verified + locked with tests (streak resets on success, 30s ceiling, transient-vs-non-transient discrimination, clean cancellation). Clock-skew: new typed `ClockSkew` model + `ExchangeAdapter.check_clock_skew()` (midpoint-sampled local-vs-server skew, redacted warn past threshold, `fetch_time`→`milliseconds()`→typed-NotSupported fallback), surfaced as a read-only `check_clock_skew` MCP tool (tool count 45→46). Exchange-downtime: `OnMaintenance`/5xx treated as transient (retried with backoff), bad-symbol/auth non-transient, exhausted retries raise a typed redacted `ExchangeError`. Partial-fill handling: `FillEvent` + `aggregate_fills` (VWAP, over-fill clamped to ordered amount, `remaining`/`partially_filled` status) + `PaperBroker.apply_fills` live entry point; `server/execution.py` testnet record now maps partial fills too. **Parity preserved** — interpreter/runtime signal logic untouched; simulated fills stay atomic (`test_simulated_fills_are_atomic_never_partial`), parity suite still 0-mismatch. Owners: exchange-adapter-engineer + backtest-engine-engineer)_
- [x] Docs: README, tool reference, **strategy spec reference**, safety guide, per-client setup (Claude Code/Cursor/Codex), disclaimers _(README rewritten Phase 0→v0.1.0 (Phases 0–6 complete, 46 tools, live gated/deferred, Coinbase default) with a Documentation index. `docs/tool-reference.md` (all 46 tools grouped per §5.2 + the `dataset://`/`strategy://`/`backtest://` resources + prompts), `docs/strategy-spec.md` (full Pydantic field reference + the complete indicator/operator/helper whitelist + 7-template catalog + worked examples, every field verified against source), `docs/safety.md` (the layered guardrail model, every deny condition cited to file:line, key scoping & secret handling, prominent Not-Financial-Advice disclaimer), `docs/client-setup.md`. Owners: mcp-server-engineer (tool ref/README/setup), strategy-spec-engineer (spec ref), risk-safety-engineer (safety guide))_
- [x] Example end-to-end workflows ("prompt → backtest → paper → testnet") _(`docs/workflows.md`: prompt-driven flows mapped to real tool names — connect/sanity (incl. `check_clock_skew`) → research market data → author from template → sync + backtest + tear sheet → paper-trade → testnet (gated); cross-references the `/author-strategy`, `/run-backtest`, `/safety-preflight` skills; notes live is walled/deferred. Owner: qa-parity-engineer)_
- [ ] Publish to PyPI; verify `uvx trader-mcp`; semantic versioning + `CHANGELOG.md` _(**Build/verify/versioning DONE; the actual PyPI publish is DEFERRED — it is outward-facing and irreversible, requiring a human with a PyPI token (not done autonomously).** Version bumped `0.0.0 → 0.1.0` (classifier `3 - Alpha` — live trading still gated/uncertified); `CHANGELOG.md` created (Keep-a-Changelog, Phases 0–7 under `[0.1.0]`). `uv build` produces a clean wheel + sdist with both entry points (`trader-mcp`, `trader-mcp-validate`); `uvx --python 3.12 --from <wheel> trader-mcp` boots the stdio server. Remaining for a human: `uv publish` with a token + tag `v0.1.0`. Owner: scaffolding-engineer)_
- [x] `.mcp.json` / client config snippets; troubleshooting guide _(`.mcp.json.example` corrected (default exchange `bybit`→`coinbase`, per-client config paths); `docs/client-setup.md` (Claude Code/Cursor/Codex step-by-step + verification); `docs/troubleshooting.md` (boot/stdio, Python 3.12 pin re numba/llvmlite/pandas-ta, geo-blocking, proxy/socks extra, `.env` not found, client not seeing tools, skipped live tests). Owners: scaffolding-engineer + mcp-server-engineer)_
- **Exit:** A new user installs via `uvx trader-mcp`, connects their AI client, and completes the example workflow from docs alone. _(**Docs + packaging are release-ready and the workflow is documented end to end; the literal `uvx trader-mcp` install path stays unchecked until the human-authorized PyPI publish above lands.** Local install path validated: `uvx --from <wheel> trader-mcp` boots; the documented workflow runs against paper/testnet with the live wall up.)_

### Phase 8 — Future / optional
- [ ] Remote **Streamable HTTP** transport + auth for multi-user/hosted deployment
- [ ] Opt-in **sandboxed code-strategies** (beyond declarative spec)
- [ ] **TradingView Strategy Tester as a third backtest cross-validation oracle** (manual CSV, ToS-clean) _(Adds a second independent reference engine beside `backtesting.py` (Phase 4, §6). **Research finding:** TradingView has no public API; the desktop app is only reachable via the undocumented Chrome DevTools Protocol debug port (the third-party `tradesdontlie/tradingview-mcp` CDP bridge) — fragile and ToS-gray for automated/non-display data use, so **explicitly out of scope**. The chosen path is a **manual export**: author the reference strategy in Pine Script v5 (mirroring the SMA-cross `StrategySpec` in `tests/test_crossvalidate_backtesting.py`), run Strategy Tester on a pinned feed/window (`COINBASE:BTCUSD 1h`, fixed date range), export the **List of Trades** CSV, and commit it under `tests/fixtures/tradingview/` with a README documenting the exact symbol/timeframe/range/settings. A new `tests/test_crossvalidate_tradingview.py` (modeled on the `backtesting.py` xval; `pytest.skip` when the fixture is absent so CI stays green) runs the same spec through `run_backtest` on cached bars and asserts **trade-level alignment** (matching count, side, entry/exit bar within ±1, per-trade return within tolerance) plus a looser aggregate-equity bound. Comparing **decisions, not OHLCV**, sidesteps TradingView↔CCXT feed differences. Calibration: start at ~5% tolerance (the existing SL/TP-path bound — TradingView's intrabar fill model differs from the engine's next-bar-open rule) and tighten empirically. Dev/test-only, like `backtesting.py`; no product-code change. Owner: qa-parity-engineer)_
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
