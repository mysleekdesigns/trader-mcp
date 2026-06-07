# Example end-to-end workflows

trader-mcp is an [MCP](https://modelcontextprotocol.io/) server. You drive it by
**prompting your AI client** (Claude Code / Cursor / Codex); the client calls the
typed MCP tools below over stdio. Nothing here is a shell command — the tool names
are the FastMCP tools the server exposes, and the AI picks and fills them from your
prompt.

Every workflow is **safe by default**: market-data and authoring tools are read-only,
backtests touch only locally cached data, and the only execution path that ships today
is `paper` (pure simulation) and a gated `testnet` (exchange sandbox, fake money).
**Real-money live trading is gated and deferred** — there is no live order path you can
reach from these workflows (see [Safety model](#safety-model)).

The reference exchange is **Coinbase** (US venues: Coinbase / Kraken / Gemini /
Crypto.com). The examples use `coinbase` and `BTC/USD`.

---

## 0. Connect and sanity-check

Prompt:

> "Connect to trader-mcp, list the exchanges you support, and check the server status."

Tools the client calls:

- `health_check` — server liveness.
- `get_server_status` — server + registered tool/resource summary.
- `list_exchanges` — supported exchanges with metadata (reference flag, tier, WS).
- `check_clock_skew` — compare local clock vs the exchange server clock (signing safety).

---

## (a) Research market data

Prompt:

> "What does Coinbase support, find me BTC markets, and show me the current BTC/USD
> ticker, the last 200 hourly candles, the order book, and recent trades."

Tools:

- `get_exchange_capabilities` (`exchange="coinbase"`) — spot/swap, OHLCV, WS, market count.
- `search_symbols` (`exchange="coinbase"`, `query="BTC"`) — fuzzy market search.
- `list_markets` (`exchange="coinbase"`, `market_type="spot"`) — full market list.
- `get_ticker` (`exchange="coinbase"`, `symbol="BTC/USD"`) — last / bid / ask / volume.
- `get_ohlcv` (`exchange="coinbase"`, `symbol="BTC/USD"`, `timeframe="1h"`, `limit=200`).
- `get_order_book` (`exchange="coinbase"`, `symbol="BTC/USD"`, `limit=25`).
- `get_recent_trades` (`exchange="coinbase"`, `symbol="BTC/USD"`, `limit=50`).
- `get_funding_rate` — perp venues only; Coinbase is spot-only, so this is N/A here.

All return typed Pydantic models (so the client gets `outputSchema`). These tools never
place orders.

---

## (b) Author a strategy from a template

Strategies are **declarative, typed specs** — never AI-generated code. Rule expressions
run through a sandboxed evaluator with whitelisted indicators/operators only.

Prompt:

> "Show me the strategy templates, explain the available indicators, then create an
> EMA-crossover strategy on Coinbase BTC/USD 1h and validate it."

Tools:

- `list_strategy_templates` — the built-in template library.
- `pick_template` / `design_strategy` — guided template selection / scaffold (MCP prompts
  `design_strategy` also exist for an interactive flow).
- `list_indicators` / `explain_indicators` — the whitelist of usable indicators.
- `create_strategy` (`spec=<StrategySpec>`) — persist a typed spec. The spec is validated
  on the way in; any non-whitelisted construct in a rule is rejected with a typed error.
- `validate_strategy` (`name=...`) — re-validate a saved spec (expressions, indicators,
  field bounds).
- `get_strategy` / `list_strategies` / `update_strategy` / `delete_strategy` — manage saved
  specs.

Saved specs are also exposed as MCP **resources** (`strategy://...`) so the client can read
them back directly.

Tip: invoke the `/author-strategy` skill for the guided authoring flow.

---

## (c) Sync history, backtest, tear sheet

Backtesting runs the **one event-driven interpreter** (the same one the live path uses)
over **real cached data** — so you must sync history first.

Prompt:

> "Sync a year of Coinbase BTC/USD 1h candles, back-test my EMA-crossover strategy on it,
> then generate a tear sheet."

Tools:

- `sync_history` (`exchange="coinbase"`, `symbol="BTC/USD"`, `timeframe="1h"`, date range)
  — fetch + cache OHLCV into the local DuckDB/Parquet store, repairing gaps.
- `list_cached_datasets` / `inspect_dataset` — confirm what is cached (also `dataset://`
  resources).
- `run_backtest` (`strategy_name=...`, `exchange`, `symbol`, `timeframe`, range) — runs the
  interpreter; returns a typed `BacktestReport` (equity curve, fills, native metrics).
- `optimize_strategy` — parameter sweeps / walk-forward (Optuna) over the same interpreter.
- `compare_backtests` — diff two reports side by side.
- `generate_tearsheet` (`report_id=...`) — quantstats HTML tear sheet (degrades gracefully
  to native metrics if rendering is unavailable).
- `get_backtest_report` (`report_id=...`) — fetch a stored report (also `backtest://`
  resources).

Tip: invoke the `/run-backtest` skill — it syncs data, runs the interpreter,
cross-validates against `backtesting.py`, runs a **backtest↔live parity** smoke check, and
renders the tear sheet.

---

## (d) Paper-trade

Paper mode is pure in-process simulation: the **same interpreter and broker logic** the
live path would use, replaying cached/streamed bars through the paper broker. No network,
no credentials, no real orders.

Prompt:

> "Start a paper session on Coinbase BTC/USD, deploy my EMA-crossover strategy to it,
> then show me the session status, positions, and P&L."

Tools:

- `start_session` (`mode="paper"`, `exchange="coinbase"`, `symbol="BTC/USD"`) — mint an idle
  session.
- `deploy_strategy` (`session_id=...`, `strategy_name=...`) — attach a saved spec; the
  runtime drives it bar-by-bar through the paper broker.
- `place_order` (`session_id=...`, ...) — a manual order; in `paper` mode the safety gate
  **always** returns `simulate` (`simulated=True`), so nothing hits an exchange.
- `get_session_status` / `get_positions` / `get_open_orders` / `get_pnl` /
  `get_trade_history` / `get_portfolio` — read the simulated state.
- `stop_strategy` (`session_id=...`) — halt the runtime.

Because paper reuses the one interpreter, paper results match the backtest decisions
bar-for-bar — that parity is enforced by the automated parity suite.

---

## (e) Testnet (gated, opt-in)

Testnet routes **real API calls to an exchange sandbox** (fake money). It is gated:

- The session mode must be `testnet`, and `start_session` jurisdiction-checks the
  (exchange, market) up front (US-eligible venues/markets only).
- An order only **routes** when the credential is **trade-enabled**, the venue/market is
  US-eligible, **and** the global dry-run default is off — otherwise the safety gate
  downgrades it to `simulate`.
- A read-only key can never place an order. A denied order raises a redacted safety error.

Prompt:

> "Set a conservative risk limit, start a testnet session on Coinbase BTC/USD, deploy my
> strategy, and place a tiny test order."

Tools:

- `set_risk_limits` (`exchange="coinbase"`, caps) — enforced **before** the gate.
- `start_session` (`mode="testnet"`, `exchange="coinbase"`, `symbol="BTC/USD"`).
- `deploy_strategy` / `place_order` — `place_order` returns an `OrderRecord` with
  `simulated=False` only when it actually routed to the sandbox.
- `cancel_order` / `get_open_orders` / `get_balance` — manage and inspect sandbox state.
- `kill_switch` — emergency stop: halts runtimes and cancels open orders.

Running this for real requires trade-enabled **sandbox** credentials (never a real-money
key). The repository's opt-in testnet integration suite (`tests/testnet/`,
`@pytest.mark.testnet`) mirrors this path and only runs when a human explicitly opts in
with sandbox keys.

---

## Safety model

These guardrails are non-negotiable invariants, not conventions:

- **One interpreter** for backtest and live/paper — decisions are identical across paths
  (verified by the parity test suite).
- **Strategy specs are data, never code** — rules go through a whitelisted sandboxed
  evaluator.
- **Dry-run by default** — `place_order` / `deploy_strategy` simulate unless the testnet
  routing conditions above are all met.
- **`live` is walled off** — `live` is not a valid session mode and the safety gate denies
  every `live` order unconditionally. `arm_live_trading` / `disarm_live_trading` /
  `get_safety_status` / `get_audit_log` exist to drive and inspect the arming state machine,
  but arming does **not** open the live wall in this release — real-money live trading is
  **gated and deferred**.
- **Secrets are radioactive** — keys come from env / `.env` / keyring, are scoped read-only
  vs trade-enabled, and are never logged (audit entries are redacted).

Run the `/safety-preflight` skill before any change that touches execution, arming, risk
limits, or credentials.
