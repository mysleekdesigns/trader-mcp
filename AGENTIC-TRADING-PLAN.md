# Agentic Trading Plan — Robinhood MCP + equities backtesting

Tracking doc for connecting Robinhood's official Trading MCP to this environment and extending
trader-mcp so rule-based equities plans can be **backtested before any real-money order**.
Check items off as work lands. This initiative changes no live-execution or safety code in
trader-mcp; the Robinhood MCP is a separate, parallel lane to trader-mcp's crypto lane.

## Research summary (crawlforge, June 2026)

Robinhood launched official MCP servers on **May 27, 2026** ("Agentic Trading" beta).
Sources: Robinhood newsroom "Robinhood is Now Open to Agents"; support articles
"Agentic Trading overview" and "Trading with your agent"; TechCrunch 2026-05-27.

- **Endpoint:** `https://agent.robinhood.com/mcp/trading` — remote MCP, Streamable HTTP, browser OAuth (no API keys).
- **Claude Code setup:** `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`,
  then `/mcp` → `robinhood-trading` → authenticate. Onboarding auto-opens to create a dedicated
  **Agentic account** (desktop browser only; requires a primary Robinhood account in good standing).
- **Containment model:** the agent gets read access to all accounts (positions, balances, order
  history) but can **only place trades in the Agentic account** with the funds deposited there.
  Push notification on every agent trade, real-time activity feed + P&L in the app, one-tap disconnect.
- **Scope today:** long US equities (fractional supported); options rolling out gradually;
  crypto/futures/event contracts "coming soon". **Real money only — no paper mode.**
- **Published tools:**
  - Account: `get_accounts`, `get_portfolio` (value, buying power, by asset class)
  - Equities: `get_equity_positions`, `get_equity_quotes` (≤20 symbols, real-time),
    `get_equity_orders`, `get_equity_tradability`, `review_equity_order` (simulates + pre-trade
    warnings), `place_equity_order`, `cancel_equity_order`, `search`
  - Watchlists: `get_watchlists`, `get_watchlist_items`, `add_to_watchlist`, `update_watchlist`,
    `get_popular_lists`, `follow_list`, `unfollow_list`
  - Options (gradual rollout): `get_option_chains`, `get_option_instruments`, `get_option_quotes`,
    `get_option_positions`, `get_option_orders`, `review_option_order`, `place_option_order`,
    `cancel_option_order`

**Honest note on "edge":** the MCP is execution + data plumbing; it confers no alpha. The edge is
process: research → backtest/optimize on real history → `review_equity_order` preview → small,
approved orders under hard risk limits. Only *rule-based* plans are backtestable (trader-mcp
specs are data with whitelisted indicators); discretionary/news-driven judgment can't be replayed
historically — rehearse those via the Alpaca MCP paper account.

## Phase 0 — Plan doc

- [x] Create this doc (`AGENTIC-TRADING-PLAN.md`) with research summary + phased checklists.

## Phase 1 — Connect Robinhood Trading MCP (read-only verification)

- [ ] Register the server: `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`.
      *(Must be run by Simon: the harness safety classifier blocks the agent from registering a
      real-money trading endpoint itself — by design. Run it with the `!` prefix in-session.)*
- [ ] **Simon (interactive):** `/mcp` → `robinhood-trading` → browser OAuth → complete Agentic-account
      onboarding (desktop) → fund the Agentic account with **only what you're willing to let an agent trade**.
- [ ] Verify: list tools; smoke-test `get_accounts`, `get_portfolio`, `get_equity_quotes` (e.g. AAPL).
      **No order placement in this phase.**
- [ ] Write `notes/robinhood-agentic-playbook.md` (operating rules: always preview + ask before place,
      position-size cap, max orders/session, research flow, lane separation from trader-mcp).
- [x] Save project memory (connection, containment model, always-preview/ask-before-place rule).

## Phase 2 — Equities backtesting in trader-mcp (`development` branch)

Delegation per CLAUDE.md: `data-pipeline-engineer` (data layer), `mcp-server-engineer` (tool surface),
`qa-parity-engineer` (tests), `code-reviewer` (merge gate).

- [ ] Dataset source type: introduce `DataSourceId = ExchangeId | Literal["external"]` for
      `DatasetKey.exchange` and store/resource paths (`src/trader_mcp/data/models.py`,
      `src/trader_mcp/config.py`). `ExchangeId` stays unchanged for all trading/adapter paths.
- [ ] New `import_ohlcv` MCP tool: typed Pydantic input (source `"external"`, symbol, timeframe,
      bars as typed rows or CSV/Parquet path); validates monotonic timestamps + OHLC sanity
      (reuse `data/quality.py`); writes via existing `OHLCVStore.upsert_bars()`; exposed as a
      `dataset://` resource. No new network dependency — Claude bridges Alpaca `get_stock_bars` → `import_ohlcv`.
- [ ] Market-hours handling: external datasets skip gap *repair*; session gaps (overnight/weekend)
      reported as informational, not errors (`src/trader_mcp/data/quality.py`). Test that the
      interpreter handles session-gapped bars (next-bar-open fills) rather than assuming it.
- [ ] End-to-end: `run_backtest`, optimize, and tear sheet work unchanged on an imported equities dataset.
- [ ] Quality gates: new unit tests (type widening; import happy path + bad-data rejection; store
      round-trip under `external`; backtest over a session-gapped equities fixture); full
      `uv run pytest` + `ruff` + `pyright` green; parity suite stays 0-mismatch; commit on `development`.

## Phase 3 — First validated-trade workflow (gated)

- [ ] Real-data pass: fetch ~2y AAPL daily bars via Alpaca MCP → `import_ohlcv` → backtest a
      mean-reversion template + Optuna sweep → tear sheet for Simon.
- [ ] Dry-run a ~$5 fractional order through `review_equity_order` **only**; show the preview +
      warnings. Nothing is placed unless Simon explicitly approves that specific order.
- [ ] Keep this doc checked off as phases complete.

## Out of scope

- Live order placement (later, per-trade, with Simon's explicit approval each time).
- Robinhood options/crypto tools (still rolling out); the Agentic Credit Card / Banking MCP.
- Any change to trader-mcp's live execution or safety modules.
- Native equities *sync* inside trader-mcp (direct Alpaca/yfinance integration) — the import-tool
  bridge is the deliberate v1; native sync is a follow-up if the workflow sticks.
