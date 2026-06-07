# MCP tool reference

The complete catalog of the trader-mcp MCP surface: **46 tools**, **6 resources**
(3 catalogs + 3 templates), and **3 prompts**, grouped as in PRD §5.2.

Every tool takes **typed Pydantic v2 inputs** and returns a **typed Pydantic v2
output** surfaced as an `outputSchema` (structured output). Inputs constrained to a
specific exchange use the closed `exchange` enum: `coinbase` (reference), `kraken`,
`gemini`, `cryptocom`. Errors are returned as redacted, typed errors — never raw
strings, never secrets.

Conventions in the tables below: **bold** marks required inputs; everything else is
optional with a default. The "Output" column names the returned Pydantic model.

---

## Connectivity / admin (3)

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `health_check` | Liveness probe. | _(none)_ | `HealthCheckResult` (status, version, timestamp) |
| `get_server_status` | Server metadata + live registered-tool count. | _(none)_ | `ServerStatusResult` (name, version, transport, started_at, uptime_seconds, tool_count) |
| `check_clock_skew` | Measure local-vs-exchange clock skew (resilience health check); flags drift past `threshold_ms`. Public, unauthenticated read. | **`exchange`**, `threshold_ms`=1000.0 | `ClockSkew` (server_time, local_time, skew_ms, round_trip_ms, threshold_ms, within_tolerance) |

## Market data & discovery (9)

All read-only public lookups; no order path. Each call resolves a shared,
rate-limited CCXT adapter for the named exchange.

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `list_exchanges` | List supported exchanges with static metadata (tier, reference flag, WS support). No network. | _(none)_ | `ExchangesResult` (exchanges, count) |
| `get_exchange_capabilities` | Report what an exchange supports + loaded market count. | **`exchange`** | `ExchangeCapabilities` |
| `search_symbols` | Case-insensitive substring search over active markets. | **`exchange`**, **`query`**, `market_type`, `limit`=50 | `SymbolSearchResult` (exchange, query, markets, count) |
| `list_markets` | List markets, optionally filtered by type/active. | **`exchange`**, `market_type`, `active_only`=true, `limit` | `MarketsResult` (exchange, markets, count) |
| `get_ticker` | Normalized ticker snapshot (last/bid/ask/volume). | **`exchange`**, **`symbol`** | `Ticker` |
| `get_ohlcv` | OHLCV candles for a symbol/timeframe. | **`exchange`**, **`symbol`**, `timeframe`=`1h`, `limit`=200, `since` | `OHLCVResult` (bars, count, ...) |
| `get_order_book` | Normalized order-book snapshot (bids/asks). | **`exchange`**, **`symbol`**, `limit`=25 | `OrderBook` |
| `get_recent_trades` | Most recent public trade prints. | **`exchange`**, **`symbol`**, `limit`=50 | `RecentTradesResult` (trades, count) |
| `get_funding_rate` | Perpetual-swap funding-rate snapshot. | **`exchange`**, **`symbol`** | `FundingRate` |

## Historical data sync (3)

The only network call is the read-only paginated OHLCV download; everything else
reads/writes the **local** DuckDB+Parquet cache. No order/arming path.

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `sync_history` | Paginated, incremental, resumable OHLCV download into the local cache; detects & (by default) repairs gaps. | **`exchange`**, **`symbol`**, **`timeframe`**, **`since`**, `until`, `page_limit`=1000, `repair_gaps`=true | `SyncResult` |
| `list_cached_datasets` | List every cached dataset (symbol, timeframe, rows, coverage, last sync). No network. | _(none)_ | `DatasetsResult` (datasets, count) |
| `inspect_dataset` | Coverage/quality report for one cached dataset (expected vs actual bars, gaps, duplicates, monotonicity). No network. | **`exchange`**, **`symbol`**, **`timeframe`** | `DatasetInspection` |

## Strategy authoring (8)

Pure local CRUD over saved strategy specs. A spec is **data, never code**: rules are
validated against the indicator/operator whitelist at the MCP boundary. No execution
path. See [`strategy-spec.md`](./strategy-spec.md) for the spec itself.

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `list_strategy_templates` | List built-in starter templates (id, title, summary, type, overridable fields). | _(none)_ | `TemplatesResult` (templates, count) |
| `list_indicators` | List the closed indicator whitelist (params + output suffixes) — the legal rule namespace. | _(none)_ | `IndicatorsResult` (indicators, count) |
| `validate_strategy` | Validate a draft spec; **never raises** — returns structured `{ok, issues:[{location, problem, fix}]}` for AI iteration. | **`spec`** (loose dict) | `ValidationReport` |
| `create_strategy` | Persist a typed `StrategySpec` (validated at the boundary against the full schema + whitelist). | **`spec`** (`StrategySpec`) | `CreateStrategyResult` (spec, info) |
| `get_strategy` | Load the full saved spec by name (re-validated on load). | **`name`** | `StrategySpec` |
| `list_strategies` | List every saved strategy with its summary. | _(none)_ | `StrategiesResult` (strategies, count) |
| `update_strategy` | Overwrite an existing strategy (name is the identity key; no rename). | **`name`**, **`spec`** | `CreateStrategyResult` (spec, info) |
| `delete_strategy` | Delete by name; idempotent (missing → `deleted=false`). | **`name`** | `DeleteResult` (name, deleted) |

## Backtest & optimize (5)

Pure simulation over the local OHLCV cache using the **same event-driven
interpreter the live path uses** (parity). No orders, no network. `sync_history`
first if no bars exist.

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `run_backtest` | Backtest a saved strategy over its cached data; persists the report (deterministic `report_id`). | **`strategy_name`**, `since`, `until`, `initial_cash`, `slippage_pct`, `seed` | `BacktestReport` |
| `get_backtest_report` | Load a saved report (metrics, trades, equity curve, config). | **`report_id`** | `BacktestReport` |
| `compare_backtests` | Side-by-side comparison of saved reports ranked by an objective metric. | **`report_ids`**, `objective`=`sharpe` | `BacktestComparison` |
| `optimize_strategy` | Optuna parameter sweep over a saved strategy (optional walk-forward folds). | **`strategy_name`**, **`params`** (`list[ParamSpec]`), `objective`=`sharpe`, `n_trials`=50, `walk_forward_folds`=0, window/fill overrides | `OptimizeResult` |
| `generate_tearsheet` | Render a quantstats HTML tear sheet for a saved report (degrades gracefully if quantstats missing). | **`report_id`**, `title` | `TearsheetResult` (html_path, metrics, note) |

## Paper / testnet execution (9)

Safe-by-default. `paper` is a pure in-process simulation (no network, no
credentials). `testnet` targets the exchange sandbox (fake money) and only **routes**
an order when the key is trade-enabled, the venue/market is US-eligible, **and** the
process-wide dry-run default is off — otherwise it is downgraded to simulate. There
is no `live` session mode. `place_order` and `deploy_strategy` run through the safety
gate ([`safety.md`](./safety.md)).

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `start_session` | Open a `paper` or `testnet` session for an exchange+symbol (testnet is jurisdiction-checked up front). | **`mode`** (`paper`\|`testnet`), **`exchange`**, **`symbol`** | `SessionInfo` |
| `place_order` | Place one order on a session, run through the safety gate first; risk limits enforced before the gate. Idempotent via `seq`. | **`session_id`**, **`symbol`**, **`side`**, **`amount`**, `type`=`market`, `price`, `reduce_only`=false, `seq` | `OrderRecord` (`simulated` = true for paper) |
| `cancel_order` | Cancel an open order by id (paper broker or testnet sandbox). | **`session_id`**, **`order_id`**, `symbol` | `CancelResult` |
| `get_open_orders` | List the open/pending orders for a session. | **`session_id`** | `OpenOrdersResult` (orders, count) |
| `get_positions` | Report open positions for a session. | **`session_id`** | `PositionsResult` (positions, count) |
| `get_balance` | Report session account balance (cash + holdings). | **`session_id`** | `PaperBalance` |
| `deploy_strategy` | Run a saved strategy on a session through the same interpreter the backtest uses (parity). | **`session_id`**, **`strategy_name`** | `SessionInfo` |
| `stop_strategy` | Stop a running session; force-close inventory for end-of-session accounting. Idempotent. | **`session_id`** | `SessionStatus` |
| `get_session_status` | Live operational snapshot (state, bars processed, orders, trades, position open). | **`session_id`** | `SessionStatus` |

## Portfolio / analytics (3)

Read-only views over a session's in-memory broker state. No orders, no network.

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `get_portfolio` | Mark-to-market snapshot (equity, cash, position value, open position, mark, last bar). | **`session_id`** | `Portfolio` |
| `get_pnl` | Realized/unrealized profit decomposition (realized, unrealized, fees, funding, total, return %). | **`session_id`** | `PnLBreakdown` |
| `get_trade_history` | Closed round-trips (entry/exit, size, pnl, fees, funding, bars held, exit reason). | **`session_id`** | `TradeHistoryResult` (trades, count) |

## Safety (6)

Configure and observe the safety machinery. Risk limits are **enforced now** on
every `place_order`; arming records the expiring opt-in state the future live wall
will require but **gates nothing yet** (live routing is deferred). See
[`safety.md`](./safety.md). No tool here accepts or echoes a secret.

| Tool | Purpose | Key inputs | Output |
| --- | --- | --- | --- |
| `set_risk_limits` | Configure per-strategy/global caps (all optional; null disables). Enforced on every order. | `max_order_notional`, `max_position_notional`, `max_open_positions`, `max_daily_loss`, `max_leverage` | `RiskLimits` |
| `arm_live_trading` | Record an expiring opt-in to live trading (requires exact phrase `I UNDERSTAND THE RISKS`). State only — no live path consumes it yet. | **`exchange`**, **`confirm`**, `ttl_seconds`=900 (≤3600) | `ArmTicket` |
| `disarm_live_trading` | Revoke an arm for a scope (or all). Idempotent. | `exchange` (null = all) | `DisarmResult` |
| `kill_switch` | Engage/reset a trading halt for a scope; on engage, best-effort cancel open paper orders. | **`action`** (`engage`\|`reset`), `exchange` (null = global), `reason` | `KillSwitchResult` (scope, engaged, orders_canceled, detail) |
| `get_safety_status` | Read-only snapshot: dry-run default, active limits, arm tickets, engaged halts, audit count. | _(none)_ | `SafetyStatus` |
| `get_audit_log` | The append-only, redacted safety audit trail (newest-last), optionally filtered. | `limit`, `event` | `AuditLogResult` (entries, count) |

---

## Resources

Cached datasets, saved strategy specs, and backtest reports are also exposed as
read-only MCP resources (returning JSON). Each group has a `catalog` resource and a
per-item template. List and read them via your client's MCP resource browser.

| Resource URI | Returns |
| --- | --- |
| `dataset://catalog` | `CatalogResult` — every cached dataset, each tagged with its resolvable per-dataset URI. |
| `dataset://{exchange}/{symbol}/{timeframe}` | `DatasetInspection` for one dataset. `{symbol}` is the **sanitized** form (`/` and `:` → `-`), e.g. `dataset://coinbase/BTC-USD/1h`. |
| `strategy://catalog` | `StrategyCatalogResult` — every saved strategy with a resolvable per-strategy URI. |
| `strategy://{name}` | Full `StrategySpec` JSON for one strategy. `{name}` is the slugified name, e.g. `strategy://ma-cross-btc`. |
| `backtest://catalog` | `BacktestCatalogResult` — every saved report with headline metrics + a resolvable per-report URI. |
| `backtest://{report_id}` | Full `BacktestReport` JSON, e.g. `backtest://a1b2c3d4`. |

## Prompts

Guided, **data-only** strategy-design flows (no code execution, no orders). They
reference the live template registry and indicator whitelist so guidance can never
drift from the implementation.

| Prompt | Purpose | Arguments |
| --- | --- | --- |
| `design_strategy` | End-to-end guided authoring: pick a template → fill typed fields within the whitelist → `validate_strategy` → `create_strategy`. | `goal`, `symbol`=`BTC/USD` |
| `pick_template` | Concise summary of the live starter templates to choose from. | _(none)_ |
| `explain_indicators` | The closed indicator whitelist (the only names a rule may reference) with each kind's outputs + allowed operators. | _(none)_ |
