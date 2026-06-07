# Safety guide

This document is the authoritative description of trader-mcp's trust model: how
the server keeps you safe by default, exactly what triggers a refusal, and what
is **not** enabled today. It is written against the code in
`src/trader_mcp/safety/` and cites `file:line` for every deny condition so it can
be checked against the source.

> **Read the [Disclaimers / Not Financial Advice](#disclaimers--not-financial-advice)
> section at the bottom before you do anything with real value at risk.**

---

## 1. Safety philosophy

Two invariants drive everything in this server:

1. **Strategy specs are data, never code.** A strategy is a declarative, typed
   Pydantic spec evaluated through a sandboxed evaluator with whitelisted
   indicators and operators only. There is no `eval`/`exec`, dynamic import, or
   arbitrary-callable path. A strategy can describe *what* to do; it can never
   execute arbitrary code on your machine or your account.

2. **Safe-by-default execution.** `place_order` and `deploy_strategy` are
   **dry-run unless explicitly armed**. Real-money live trading is gated and, in
   this build, **deferred** — the live wall is up and cannot be opened by any MCP
   tool. Keys are scoped read-only vs trade-enabled, and secrets are never logged
   or returned.

Every order intent passes through a single chokepoint that **fails closed**: if
anything is uncertain, the order is denied rather than placed.

### The layered model

An order intent is evaluated top-to-bottom in
`SafetyController.preflight` (`src/trader_mcp/safety/controller.py:166`). The
**first** failing layer wins, and every layer fails closed:

```
order intent
   │
   ▼
[1] Kill switch        ── halted for this scope?            → DENY
   │ (controller.py:195)
   ▼
[2] Risk limits        ── any configured cap breached?      → DENY
   │ (controller.py:215)
   ▼
[3] The gate           ── evaluate_order: mode × key-scope × jurisdiction × dry-run
   │ (controller.py:233 → policy.py:107)
   │      • paper   → simulate (always)
   │      • testnet → route only if trade-enabled key + US-eligible venue + dry-run off
   │      • live    → DENY unconditionally  ◀── the live wall
   ▼
[4] Audit             ── record the intent + decision (redacted)
   │ (controller.py:244)
   ▼
decision: simulate | route | deny   (the caller honors the verdict)
```

The controller **never routes a real order itself**. It returns a `GateDecision`
(`simulate` / `route` / `deny`) and the caller is required to honor it.

---

## 2. The live wall (deferred — currently UP)

**Real-money live trading is not enabled in this build.** This is not a setting
you can flip from a prompt; it is enforced structurally in three independent
places:

- **The gate denies `live` unconditionally.** In
  `evaluate_order`, the `live` branch always returns `action="deny"`, regardless
  of arm state, key scope, or any flag — `src/trader_mcp/safety/policy.py:174-186`.
  The `armed` parameter exists but is explicitly ignored on this branch
  (`policy.py:138-141`, `policy.py:174-181`).
- **There is no `live` session mode at the MCP surface.**
  `ServerSessionMode` is `Literal["paper", "testnet"]` only —
  `src/trader_mcp/server/execution.py:85`. No MCP tool can open a live session.
- **The arming state machine is not wired to the wall.** The
  `arm_live_trading` tool drives the arm *state* and audits it, but no live order
  path consumes that state. The controller documents this directly
  (`src/trader_mcp/safety/controller.py:3-9`, `:89-102`) and so does the gate
  (`policy.py:174-186`).

Opening this wall is the deferred live-certification step. Until then, the most
an intent can ever do against a real venue is route to an exchange **testnet
sandbox with fake money**, and only with the global dry-run switch explicitly
turned off.

---

## 3. Layer 1 — the kill switch

**File:** `src/trader_mcp/safety/kill_switch.py`,
controller methods in `src/trader_mcp/safety/controller.py:134-162`.

The kill switch is the hardest stop and is checked **first** in preflight. It
models a global halt plus a set of per-exchange halts.

- **Global halt** (`engage(exchange=None)`) stops **every** exchange —
  `kill_switch.py:43-46`, halts read at `kill_switch.py:63-64`.
- **Per-exchange halt** (`engage(exchange="coinbase")`) stops only that venue —
  `kill_switch.py:47`, `kill_switch.py:65`.
- A global halt takes precedence and surfaces its reason — `kill_switch.py:80-82`.
- `reset(exchange=None)` clears the global halt **and** every per-exchange halt;
  resetting an unengaged scope is a no-op — `kill_switch.py:54-59`.

**Deny condition:** in `SafetyController.preflight`, if
`self._kill_switch.is_halted(exchange)` is true the intent is denied immediately
with reason `"kill switch engaged: trading halted for this scope"` (plus any
stored reason) and a `denied` audit entry is recorded —
`src/trader_mcp/safety/controller.py:195-212`.

**Cancel-all / flatten:** the `kill_switch` MCP tool's `engage` action also
best-effort **cancels open paper orders** across in-scope sessions and returns
the count — `src/trader_mcp/server/safety.py:233-265`. Testnet cancel-all is a
documented deferral. Cancels still work while halted, so a halted scope can be
cleaned up (`safety.py:206-213`).

---

## 4. Layer 2 — risk limits

**File:** `src/trader_mcp/safety/risk.py`. MCP tool: `set_risk_limits`
(`src/trader_mcp/server/safety.py:123-154`).

Risk limits are **enforced server-side, never advisory.** They are checked on
every order intent *before* the gate (`controller.py:214-230`) and are enforced
**today** on paper/testnet `place_order` — an over-limit order is denied now, and
the same limits will gate live orders once live is certified
(`safety.py:128-135`).

### The limits (all optional; `None` disables the cap)

| Limit | Field | What it caps |
| --- | --- | --- |
| Max order notional | `max_order_notional` | a single order's notional (quote ccy) |
| Max position notional | `max_position_notional` | the resulting position's absolute notional |
| Max open positions | `max_open_positions` | concurrent open positions |
| Max daily loss | `max_daily_loss` | realized loss magnitude per UTC day |
| Max leverage | `max_leverage` | order/position leverage |

Defined in `RiskLimits` — `src/trader_mcp/safety/risk.py:21-42`.

### How `None` and boundaries behave

- **`None` disables a cap.** Each `None` limit is skipped entirely —
  documented at `risk.py:7-11` and applied in each `is not None` guard below.
- **Boundary is inclusive: `== limit` is allowed, only `> limit` breaches.**
  Every check uses strict `>`, so an order whose value exactly equals the cap
  passes — `risk.py:8-11`.
- A configured cap must be non-negative; a negative value is rejected at
  construction — `risk.py:44-61`.

### Each deny condition (from `check_order_risk`, `risk.py:109-163`)

The check is a pure function that returns a `RiskCheck`; each breached cap adds
exactly one violation string, and the order passes only when there are none. In
the controller, **any** violation denies the order with reason
`"risk limit breached: " + "; ".join(violations)` — `controller.py:215-230`.

1. **Order notional > `max_order_notional`** → violation —
   `src/trader_mcp/safety/risk.py:126-130`.
2. **Resulting position notional > `max_position_notional`** → violation —
   `src/trader_mcp/safety/risk.py:132-139`.
3. **Projected open positions > `max_open_positions`** → violation. The
   projection adds 1 to the pre-order count when the order opens a new position —
   `src/trader_mcp/safety/risk.py:141-148`.
4. **Realized loss today > `max_daily_loss`** → violation —
   `src/trader_mcp/safety/risk.py:150-154`.
5. **Leverage > `max_leverage`** → violation (only when leverage is known) —
   `src/trader_mcp/safety/risk.py:156-161`.

### The unpriceable market order rule

The two notional caps need a **known price** to evaluate. A limit order supplies
its price, and a session with market data supplies a position mark. A **market
order with no derivable price, while a notional cap is set, is denied
(fail-closed)** — documented on the `set_risk_limits` tool at
`src/trader_mcp/server/safety.py:131-135`. The server will not let an order whose
notional it cannot compute slip past a notional cap.

### Setting limits

Call `set_risk_limits` with any subset of the five fields; pass `null` (omit) to
leave a cap disabled. The call replaces the active limits, records an audit
entry, and returns the stored `RiskLimits` —
`controller.py:73-83`, `safety.py:139-154`. Inspect the active limits at any time
via `get_safety_status`.

---

## 5. Layer 3 — the gate (`evaluate_order`)

**File:** `src/trader_mcp/safety/policy.py:107-219`.

The gate is the single chokepoint that decides `simulate` / `route` / `deny` from
mode × key-scope × jurisdiction × dry-run. Its decision matrix:

| Mode | Condition | Action |
| --- | --- | --- |
| `paper` | always (no key/venue needed) | **simulate** (`policy.py:166-172`) |
| `testnet` | read-only key | **deny** (`policy.py:188-193`) |
| `testnet` | ineligible venue/market | **deny** (`policy.py:195-198`) |
| `testnet` | trade-enabled key + US-eligible + dry-run **on** | **simulate** (downgrade) (`policy.py:200-206`) |
| `testnet` | trade-enabled key + US-eligible + dry-run **off** | **route** (sandbox, fake money) (`policy.py:207-212`) |
| `live` | always | **deny** — the live wall (`policy.py:174-186`) |
| unknown mode | anything | **deny** (fail closed) (`policy.py:156-162`) |
| negative amount/notional | anything | **deny** (`policy.py:149-154`) |

### Global dry-run (safe by default)

The process-wide dry-run default is **on** unless `TRADER_MCP_DRY_RUN` is
explicitly set to a disabling value (`0`/`false`/`no`/`off`) —
`global_dry_run`, `src/trader_mcp/safety/policy.py:59-69`. While it is on, even an
otherwise-routable testnet decision is downgraded to `simulate`. This is what
makes "dry-run by default" hold without any per-call configuration.

---

## 6. Key scoping & secret handling

### Read-only vs trade-enabled keys

**File:** `src/trader_mcp/safety/scoping.py`; enum in
`src/trader_mcp/config.py:33-45`.

Credentials are scoped `read_only` or `trade_enabled`, and **default to
`read_only`** (`config.py:36-45`). A read-only credential must be *structurally*
incapable of trading:

- Any trade-routing path calls `require_trade_scope` first, which **raises** a
  `SafetyError` for a read-only scope before any exchange `createOrder` is even
  constructed — `src/trader_mcp/safety/scoping.py:49-67`. It raises rather than
  returning a flag precisely so a read-only key can never reach an order call.
- At the gate, a `testnet` order with a read-only key is denied —
  `policy.py:188-193`.

### Jurisdiction allowlist

`check_jurisdiction` enforces the v1 venue/market allowlist (US-eligible venues
only: Coinbase, Kraken, Gemini, Crypto.com) and restricts perpetual (`swap`)
markets to CFTC-regulated venues — `src/trader_mcp/safety/scoping.py:70-101`.
A non-eligible venue or an ineligible perp request raises a `SafetyError`, which
the gate turns into a `deny` (`policy.py:195-198`). It returns a standing
disclaimer string attached to routed decisions (`scoping.py:42-46`).

### Secrets are radioactive

**File:** `src/trader_mcp/logging_config.py`.

Secrets come from env / `.env` / OS keyring only and are **never logged, printed,
or returned in tool output.** This is enforced by a redaction filter applied to
**every** log record:

- `RedactionFilter` renders each record's final message and runs it through
  `redact()` before emission, then clears the args —
  `src/trader_mcp/logging_config.py:118-142`.
- `redact()` scrubs, in order: URL-embedded credentials
  (`scheme://user:pass@host`), `Authorization`/`Proxy-Authorization` headers,
  bare bearer tokens, keyed secrets (`api_key=...`, `secret: ...`, etc.), and
  long opaque high-entropy tokens (≥32 chars) — `logging_config.py:99-115`.
- Logs go to **stderr**, so they never corrupt the MCP stdio (stdout) JSON-RPC
  channel — `logging_config.py:173-184`.
- No safety MCP tool accepts or echoes a secret — `src/trader_mcp/server/safety.py:24-26`.

If a secret ever leaks into an upstream error string, the redaction filter (and
the audit redaction below) is the defense that keeps it out of the logs.

---

## 7. Arming (built, expiring, confirmation-gated — gates nothing yet)

**File:** `src/trader_mcp/safety/arming.py`. Controller: `controller.py:89-130`.
MCP tools: `arm_live_trading`, `disarm_live_trading`
(`src/trader_mcp/server/safety.py:156-201`).

The arming state machine is the explicit, expiring opt-in the *future* live wall
will require. It is fully built and unit-tested, but **today it gates nothing** —
no live order path consumes it (`arming.py:1-8`, `safety.py:14-18`).

How it works:

- **Exact confirmation phrase required.** Arming requires the phrase
  `I UNDERSTAND THE RISKS` *exactly* (`REQUIRED_CONFIRMATION`,
  `arming.py:29`). A mismatch raises a `SafetyError` and fails closed —
  `arming.py:114-117`.
- **Always expires.** The default TTL is 900 s (15 min) and is **clamped to a
  hard ceiling of 3600 s** — `DEFAULT_ARM_TTL_SECONDS` / `MAX_ARM_TTL_SECONDS`,
  `arming.py:34-38`; clamp at `arming.py:118-120`. A non-positive TTL is rejected
  (`arming.py:118-119`). Expired tickets are pruned automatically
  (`arming.py:141-163`).
- **Per-exchange or global.** Arm a single exchange, or the sentinel `*`
  (`GLOBAL_SCOPE`) which covers every exchange — `arming.py:31-32`,
  `arming.py:141-153`.
- **Revocable.** `disarm_live_trading` revokes one scope, or omit the exchange
  to disarm **all** scopes; idempotent — `arming.py:131-139`,
  `safety.py:184-201`.
- Every arm/disarm is audited — `controller.py:106-126`.

Because the gate's `live` branch ignores arm state, calling `arm_live_trading`
will never cause a real order to route in this build.

---

## 8. The audit log

**File:** `src/trader_mcp/safety/audit.py`. MCP tool: `get_audit_log`
(`src/trader_mcp/server/safety.py:281-297`).

Every order intent/result and every safety state change is recorded to an
append-only, **redacted** audit trail:

- The free-text `detail` field is **always redacted on record** via the same
  `redact()` used for logging, so a secret embedded in an upstream error string
  can never be persisted in the clear — `src/trader_mcp/safety/audit.py:70-80`.
  Structured fields are typed/enum values that never carry secret material
  (`audit.py:71-76`).
- It is an in-memory, bounded ring buffer (default capacity 10,000); newest
  entries survive, oldest are evicted — `audit.py:22-23`, `audit.py:65-79`.
- Recorded event types: `order_intent`, `order_result`, `denied`,
  `kill_switch`, `arm`, `disarm`, `set_risk_limits` — `audit.py:25-34`.
- Read it newest-last, optionally capped by `limit` and filtered by `event` —
  `audit.py:82-94`, `safety.py:294-297`.

The controller records a `denied` entry for every kill-switch or risk-limit
refusal (`controller.py:201-211`, `:219-229`) and an `order_intent` entry
carrying the gate decision for everything that passes (`controller.py:244-254`).

---

## 9. The safety MCP tools

All defined in `src/trader_mcp/server/safety.py`; each takes Pydantic-validated
input and returns a frozen Pydantic model (`outputSchema` emitted).

| Tool | Purpose | Source |
| --- | --- | --- |
| `set_risk_limits` | Configure the five risk caps (enforced today on paper/testnet) | `safety.py:123-154` |
| `arm_live_trading` | Drive the expiring, confirmation-gated arm state (gates nothing yet) | `safety.py:156-182` |
| `disarm_live_trading` | Revoke an arm (one scope or all) | `safety.py:184-201` |
| `kill_switch` | Engage/reset a global or per-exchange halt; engage cancels open paper orders | `safety.py:203-241` |
| `get_safety_status` | Read-only snapshot: dry-run, limits, active arms, halted scopes, audit count | `safety.py:267-279` |
| `get_audit_log` | Read the redacted append-only audit trail | `safety.py:281-297` |

---

## Disclaimers / Not Financial Advice

**Not financial advice.** This software is provided **"as is"**, without warranty
of any kind, express or implied, including but not limited to the warranties of
merchantability, fitness for a particular purpose, and non-infringement. Nothing
produced by this software — including strategies, backtests, signals, or any
output — is a recommendation to buy, sell, or hold any asset, and nothing here
constitutes financial, investment, legal, or tax advice.

**Trading carries substantial risk.** Trading cryptocurrencies, derivatives, and
other instruments carries a substantial risk of loss and is not suitable for
every person. Past performance — including any backtested or simulated result —
is **not** indicative of future results. Markets, exchanges, and APIs can fail,
lag, or behave unexpectedly. You can lose some or all of your capital.

**You assume all responsibility and risk.** You assume full responsibility and
all risk for any use of this software, including any orders it places or fails to
place, any configuration of risk limits, arming, or the kill switch, and any
consequences thereof. The safety guardrails described here are best-effort
engineering controls, **not** a guarantee against loss, error, or malfunction.
You are responsible for understanding your own legal and regulatory obligations,
for the security of your API keys, and for verifying every action before any real
value is at risk.

**No liability.** To the maximum extent permitted by law, the authors and
contributors are not liable for any losses, damages, or claims of any kind
arising from the use of, or inability to use, this software.

**Real-money trading is deferred in this build.** As described in
[The live wall](#2-the-live-wall-deferred--currently-up), no MCP tool can place a
real-money live order in this version. Do not assume any control described here
permits live trading until that wall is explicitly lifted and independently
verified.
