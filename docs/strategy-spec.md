# Strategy spec reference

This is the authoritative reference for the **trader-mcp strategy spec**: the
declarative contract that describes a trading strategy. Read it to author a valid
spec by hand, to understand what a rule expression may legally contain, or to map
a validation error back to the field that caused it.

> Looking for a guided, step-by-step flow instead of a reference? Use the
> [`/author-strategy`](../.claude/skills/author-strategy/SKILL.md) skill, which
> walks you from a template through validation to a dry-run backtest. This
> document is the underlying contract that flow fills in.

---

## 1. Overview & core principles

A strategy in trader-mcp is **declarative, typed data — never executable code.**
The whole strategy is a single frozen [Pydantic v2](https://docs.pydantic.dev/)
model, `StrategySpec`, defined in
[`src/trader_mcp/strategy/spec.py`](../src/trader_mcp/strategy/spec.py). It is
JSON-serializable, JSON-schema-able, and carries everything needed to run the
strategy: the instrument and timeframe, the indicators to compute, the
entry/exit rule expressions, position sizing, risk limits, and fees.

Three invariants follow from "specs are data, not code", and they shape every
field below:

1. **No arbitrary code execution.** Rule expressions are plain strings. They are
   never `eval`/`exec`'d as Python. Each is parsed into an AST and walked
   node-by-node against a strict **default-deny whitelist** of operators,
   indicator/OHLCV names, and a handful of helper functions. Anything outside the
   whitelist is hard-rejected at spec-construction time, so an unsafe spec can
   never be built. The whitelist is a **security boundary** — see §4.

2. **One interpreter for backtest and live.** The exact same `StrategySpec`
   object is consumed by the backtest interpreter and the live runtime. The spec
   is **execution-agnostic**: nothing in it knows about bars, fills, or order
   routing. This guarantees backtest↔live parity — the strategy you backtest is
   bit-for-bit the strategy you run live.

3. **Determinism.** Every whitelisted indicator is deterministic and pure: the
   same bars in always produce the same output columns. Backtest and live
   therefore see identical indicator values.

### The three strategy shapes (`strategy_type`)

Not every strategy fits an entry/exit-rule shape, so the spec uses a
discriminator field, `strategy_type`:

| `strategy_type` | Meaning | Required block | Rules |
| --- | --- | --- | --- |
| `"rule"` (default) | Entry/exit rule expressions generate orders | none | at least one entry/exit rule required; `grid`/`dca` must be absent |
| `"grid"` | A price-grid bot (staggered limit orders in a band) | `grid` block required | entry/exit rules optional; `dca` must be absent |
| `"dca"` | Dollar-cost averaging on a fixed cadence | `dca` block required | entry/exit rules optional; `grid` must be absent |

Most authoring is for `"rule"` strategies; that is the focus of this document.

### How a spec is built and validated

Construct a `StrategySpec` directly (it validates in its model validator), or go
through the richer entry points in
[`src/trader_mcp/strategy/validation.py`](../src/trader_mcp/strategy/validation.py):

- `create_strategy(data: dict) -> StrategySpec` — validate and return the spec,
  or raise a `ValidationError` carrying structured `{location, problem, fix}`
  issues.
- `validate_strategy(data) -> ValidationReport` — never raises; returns
  `ok: bool` plus a list of actionable issues. This is what the
  `validate_strategy` MCP tool wraps.

Start from a template (§5) with `build_from_template(template_id, overrides)`
rather than authoring from scratch.

---

## 2. Field reference

All spec models are **frozen** and **`extra="forbid"`** — an unknown field is a
hard error, not silently ignored. Models live in
[`src/trader_mcp/strategy/spec.py`](../src/trader_mcp/strategy/spec.py).

### 2.1 `StrategySpec` (top level)

Defined at [`spec.py:194`](../src/trader_mcp/strategy/spec.py).

| Field | Type | Default | Constraints / notes |
| --- | --- | --- | --- |
| `schema_version` | `str` | `"1.0"` | Spec schema version (`SCHEMA_VERSION`). Owned by the model/store; not a caller override. |
| `name` | `str` | — (required) | 1–128 chars. |
| `description` | `str \| None` | `None` | ≤ 2000 chars. |
| `exchange` | `ExchangeId` | `"coinbase"` | One of `coinbase`, `kraken`, `gemini`, `cryptocom`. |
| `symbol` | `str` | — (required) | Non-empty CCXT symbol, e.g. `"BTC/USD"`. US venues quote USD/USDC. |
| `timeframe` | `str` | `"1h"` | Must be a supported timeframe (see §2.2). |
| `strategy_type` | `"rule" \| "grid" \| "dca"` | `"rule"` | The discriminator (see §1). |
| `indicators` | `list[IndicatorSpec]` | `[]` | Indicator instances; ids must be unique. See §2.3. |
| `entry` | `EntryRules` | empty `EntryRules` | Long/short entry expressions. See §2.4. |
| `exit` | `ExitRules` | empty `ExitRules` | Long/short exit expressions. See §2.4. |
| `position_sizing` | `PositionSizing` | `percent_equity` @ `5.0` | See §2.5. |
| `risk` | `RiskLimits` | empty `RiskLimits` | See §2.6. |
| `fees` | `Fees` | taker `0.0006`, maker `0.0002` | See §2.7. |
| `grid` | `GridConfig \| None` | `None` | Required iff `strategy_type == "grid"`. See §2.8. |
| `dca` | `DCAConfig \| None` | `None` | Required iff `strategy_type == "dca"`. See §2.9. |

**Cross-field validation** (runs automatically on construction,
[`spec.py:225`](../src/trader_mcp/strategy/spec.py)):

- `timeframe` must be on the supported whitelist.
- Indicator `id`s must be unique.
- The discriminated block must match `strategy_type` (correct block present, the
  other absent; `"rule"` requires ≥ 1 rule).
- Every entry/exit rule expression must pass the safe-evaluator whitelist check,
  and every name it references must resolve to an OHLCV column or a defined
  indicator output.

### 2.2 Supported timeframes

From [`src/trader_mcp/data/timeframes.py`](../src/trader_mcp/data/timeframes.py)
(`TIMEFRAME_MS`):

```
1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w
```

Any other value is rejected.

### 2.3 `IndicatorSpec`

One indicator instance the strategy computes
([`spec.py:61`](../src/trader_mcp/strategy/spec.py)).

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `id` | `str` | — (required) | Min length 1. Must be a valid Python identifier and **must not** start with `__`. This is the handle rules reference and the base of the output column names. |
| `kind` | `IndicatorKind` | — (required) | One of the whitelisted kinds (§4.4). |
| `params` | `dict[str, float]` | `{}` | Overrides for the kind's params. Unknown keys are rejected; integer params must be positive integers; float params must be positive. Omitted params use the registry default. |

**Output column naming** (the contract rules rely on). Defined in
[`registry.py`](../src/trader_mcp/indicators/registry.py),
`output_names_for`:

- A **single-output** indicator (e.g. `sma`, `ema`, `rsi`, `atr`) contributes one
  column named exactly the indicator's `id`.
- A **multi-output** indicator contributes `{id}` for its primary line plus
  `{id}_{suffix}` for each additional line, using the fixed suffixes in §4.4.

Example: `{id: "m", kind: "macd"}` contributes `m` (MACD line), `m_signal`, and
`m_hist`. `{id: "bb", kind: "bbands"}` contributes `bb` (middle), `bb_upper`,
`bb_lower`.

This means an author writes `m_signal > 0` and never needs to know pandas-ta's
internal column strings (`MACDs_12_26_9`).

### 2.4 `EntryRules` / `ExitRules`

Both have the same shape ([`spec.py:109`](../src/trader_mcp/strategy/spec.py)):

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `long` | `str \| None` | `None` | Boolean rule expression for the long side. `None` = no rule on this side. |
| `short` | `str \| None` | `None` | Boolean rule expression for the short side. `None` = no rule on this side. |

Use `None` (JSON `null`) to mean "no rule" — an **empty string is rejected** by
the evaluator. A `"rule"` strategy needs at least one of the four
(`entry.long`, `entry.short`, `exit.long`, `exit.short`) to be non-null.

### 2.5 `PositionSizing`

How much to allocate per position
([`spec.py:123`](../src/trader_mcp/strategy/spec.py)).

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `mode` | `"percent_equity" \| "fixed_quote" \| "fixed_base"` | `"percent_equity"` | — |
| `value` | `float` | — (required) | `> 0`. If `mode == "percent_equity"`, also `<= 100`. |

- `percent_equity` — `value` percent of account equity.
- `fixed_quote` — `value` units of the quote currency (e.g. USD).
- `fixed_base` — `value` units of the base currency (e.g. BTC).

### 2.6 `RiskLimits`

Per-strategy risk parameters; all optional
([`spec.py:141`](../src/trader_mcp/strategy/spec.py)). These are the spec-side
declarations; the safety engine enforces account-level limits separately.

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `stop_loss_pct` | `float \| None` | `None` | `> 0` and `<= 100`. Percent of entry price. |
| `take_profit_pct` | `float \| None` | `None` | `> 0`. Percent of entry price. |
| `max_leverage` | `float \| None` | `None` | `>= 1` and `<= 125`. US-legal perps only; leave `None` (or 1) for spot. |

### 2.7 `Fees`

Taker/maker fee fractions ([`spec.py:155`](../src/trader_mcp/strategy/spec.py)).

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `taker` | `float` | `0.0006` | `>= 0` and `<= 1` (e.g. `0.0006` = 0.06%). |
| `maker` | `float` | `0.0002` | `>= 0` and `<= 1`. |

### 2.8 `GridConfig` (only when `strategy_type == "grid"`)

Evenly spaced buy/sell levels in a band
([`spec.py:162`](../src/trader_mcp/strategy/spec.py)).

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `lower` | `float` | — (required) | `> 0`. Quote-price lower bound. |
| `upper` | `float` | — (required) | `> 0`. Must be `> lower`. |
| `levels` | `int` | — (required) | `>= 2` and `<= 200`. Number of grid lines. |
| `allocation_pct` | `float` | `50.0` | `> 0` and `<= 100`. Share of equity the grid uses. |

### 2.9 `DCAConfig` (only when `strategy_type == "dca"`)

Buy a fixed amount every N bars
([`spec.py:182`](../src/trader_mcp/strategy/spec.py)).

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `amount_quote` | `float` | — (required) | `> 0`. Quote-currency spend per purchase. |
| `interval_bars` | `int` | — (required) | `>= 1`. Cadence in bars. |
| `max_purchases` | `int \| None` | `None` | `>= 1` if set. Caps total buys. |

---

## 3. JSON schema

A machine-readable JSON Schema for `StrategySpec` is available at runtime via
`spec_json_schema()` ([`spec.py:287`](../src/trader_mcp/strategy/spec.py)), which
returns `StrategySpec.model_json_schema()`. The MCP server exposes this as the
`outputSchema` of the strategy-authoring tools. Treat the live schema as
canonical if it ever diverges from the tables above.

---

## 4. Rule-expression grammar & the whitelist

This section is the **security boundary**. The evaluator lives in
[`src/trader_mcp/strategy/evaluator.py`](../src/trader_mcp/strategy/evaluator.py).
A rule expression is parsed with `ast.parse(expr, mode="eval")` and then every
AST node is checked against an allowlist (`validate_expression`,
[`evaluator.py:312`](../src/trader_mcp/strategy/evaluator.py)). The check is
**default-deny**: any node type not explicitly allowed is rejected. There is no
`eval`/`exec`, no `__builtins__`, no attribute traversal — so there is no escape
to arbitrary Python.

A rule is a single **boolean expression**. It evaluates to `True`/`False` (a
signal) per bar.

### 4.1 Allowed operators

| Category | Operators | Notes |
| --- | --- | --- |
| Boolean | `and`, `or` | Combine conditions. |
| Unary | `not`, unary `+`, unary `-` | |
| Arithmetic (binary) | `+` `-` `*` `/` `%` | Division/modulo **by zero → NaN → `False`** in both scalar and Series mode (no parity divergence). |
| Comparison | `<` `<=` `>` `>=` `==` `!=` | Chained comparisons allowed, e.g. `30 < rsi < 70`. |

### 4.2 Allowed operands

- **Names** — only names in the allowed set: the OHLCV columns plus the spec's
  indicator output names (see §4.3). Any other name is rejected.
- **Constants** — numbers (`int`/`float`) and booleans (`True`/`False`/`None`).
  **Strings/bytes are not allowed.**

### 4.3 Allowed names (the namespace)

The OHLCV columns are always available
(`OHLCV_COLUMNS`, [`registry.py:66`](../src/trader_mcp/indicators/registry.py)):

```
open, high, low, close, volume
```

Plus every output name contributed by the spec's `indicators` (per the naming
convention in §2.3 / §4.4). `allowed_names_for(indicators)`
([`registry.py:390`](../src/trader_mcp/indicators/registry.py)) computes the full
namespace; the spec validator rejects any rule referencing a name outside it.

### 4.4 Whitelisted helper functions

These are the **only** callables. Each has an exact required arity
(`_HELPER_ARITY`, [`evaluator.py:74`](../src/trader_mcp/strategy/evaluator.py)).
Any other function call — or an indirect/computed call target, or keyword
arguments — is rejected.

| Call | Arity | Meaning |
| --- | --- | --- |
| `crossover(a, b)` | 2 | `True` on bar *t* when `a` was `<= b` on *t-1* **and** `a > b` on *t* (a crosses strictly above b). First bar is never a crossover. |
| `crossunder(a, b)` | 2 | `True` on bar *t* when `a` was `>= b` on *t-1* **and** `a < b` on *t* (a crosses strictly below b). First bar is never a crossunder. |
| `abs(x)` | 1 | Absolute value. |
| `min(a, b)` | 2 | **Element-wise binary** minimum only. |
| `max(a, b)` | 2 | **Element-wise binary** maximum only. |

> `min`/`max` are deliberately the **two-argument element-wise** forms only. A
> single-arg `min(close)` is rejected: reducing over a whole column would leak
> future bars (lookahead) and break scalar/Series parity.

### 4.5 What is NOT allowed (and why)

Each of these is rejected with a clear "what + how to fix" message
(see the explicit rejections in
[`evaluator.py:126`](../src/trader_mcp/strategy/evaluator.py)):

| Construct | Example | Why rejected |
| --- | --- | --- |
| Attribute access | `x.y` | No dotted access — prevents attribute escapes to arbitrary objects. |
| Subscripting | `x[0]` | Could index history/leak future bars; use the cross helpers. |
| Lambdas / function defs | `lambda x: x` | Strategies are data, not code. |
| Comprehensions / generators | `[a for a in b]` | Not a flat boolean rule. |
| Walrus | `x := 1` | No assignment; rules are expressions only. |
| f-strings / strings | `f"{x}"`, `"hi"` | Rules contain no strings. |
| Collection literals | `[1,2]`, `(1,2)`, `{1}`, `{...}` | Compare scalars, not collections. |
| Starred / unpacking args | `f(*xs)` | Pass exactly the positional args a helper expects. |
| Conditional expressions | `a if c else b` | Use `and`/`or`/`not` instead. |
| Dunder names | `__class__` | Forbidden — blocks introspection escapes. |
| Power / floor-div / bitwise / shift | `**`, `//`, `&`, `|`, `<<` | Not in the arithmetic allowlist. |
| `is`, `in` | `x is None` | Not in the comparison allowlist. |
| Unknown / non-whitelisted function | `sqrt(x)`, `print(x)` | Only the five helpers in §4.4 are callable. |
| Any unknown name | `foo` | Must resolve to an OHLCV column or a defined indicator output. |
| Empty expression | `""` | Use `null` to mean "no rule", not an empty string. |

### 4.6 Indicator catalog (whitelisted kinds)

The closed set of indicator kinds (`IndicatorKind` /  `INDICATOR_REGISTRY`,
[`registry.py:52`](../src/trader_mcp/indicators/registry.py)), backed by
`pandas-ta` through the single import seam
[`src/trader_mcp/indicators/_ta.py`](../src/trader_mcp/indicators/_ta.py).
Adding a kind here (with tests) is the **only** way to extend what rules can
reference.

| `kind` | Summary | Params (default) | Output columns (`id` + suffixes) |
| --- | --- | --- | --- |
| `sma` | Simple moving average of close | `length` (20) | `id` |
| `ema` | Exponential moving average of close | `length` (20) | `id` |
| `rsi` | Relative Strength Index of close (0–100) | `length` (14) | `id` |
| `macd` | MACD line, signal, histogram | `fast` (12), `slow` (26), `signal` (9) | `id`, `id_signal`, `id_hist` |
| `bbands` | Bollinger Bands | `length` (20), `std` (2.0, float) | `id` (middle), `id_upper`, `id_lower` |
| `donchian` | Donchian channel | `length` (20) | `id` (middle), `id_upper`, `id_lower` |
| `atr` | Average True Range (volatility) | `length` (14) | `id` |
| `stoch` | Stochastic oscillator | `k` (14), `d` (3), `smooth_k` (3) | `id` (%K), `id_d` (%D) |
| `adx` | Average Directional Index with +DI/-DI | `length` (14) | `id`, `id_plus_di`, `id_minus_di` |

All `length`/`fast`/`slow`/`signal`/`k`/`d`/`smooth_k` params are positive
integers; `std` is a positive float. Unknown param keys are rejected per §2.3.

> **Warmup / lookback.** Each indicator needs enough leading bars before it
> produces a value; until then it is NaN, and a NaN comparison yields `False`
> (no signal) — identically in backtest and live. Roughly, `sma`/`ema`/`rsi`/
> `atr` warm up over `length` bars; `macd` over `slow + signal`; `bbands`/
> `donchian` over `length`; `stoch` over `k + smooth_k + d`; `adx` over ~`2 ×
> length`. Make sure your data range comfortably exceeds the warmup of the
> longest-lookback indicator.

---

## 5. Template catalog

Seven curated, pre-validated templates live in
[`src/trader_mcp/strategy/templates.py`](../src/trader_mcp/strategy/templates.py).
Each `build_from_template(id, overrides)` returns a fresh, valid `StrategySpec`.
All default to **Coinbase `BTC/USD` `1h` spot** with conservative risk. Overrides
are a shallow merge over the top-level spec fields, then re-validated.

| `id` | `strategy_type` | Purpose | Key indicators / config & rules |
| --- | --- | --- | --- |
| `ma_cross` | rule | Fast/slow EMA crossover (trend following) | `fast` EMA(20), `slow` EMA(50); long `crossover(fast, slow)`, short `crossunder(fast, slow)`; exits reverse |
| `rsi_reversion` | rule | RSI mean-reversion (buy oversold) | `rsi` RSI(14); entry `rsi < 30` / `rsi > 70`; exit `rsi > 50` / `rsi < 50` |
| `donchian_break` | rule | Donchian channel breakout | `dc` donchian(20); entry `close > dc_upper` / `close < dc_lower`; exit `close < dc` / `close > dc` |
| `macd` | rule | MACD signal-line crossover | `m` macd(12,26,9); entry `crossover(m, m_signal)` / `crossunder(m, m_signal)`; exits reverse |
| `bollinger` | rule | Bollinger Band mean-reversion | `bb` bbands(20, 2.0); entry `close < bb_lower` / `close > bb_upper`; exit `close > bb` / `close < bb` |
| `grid` | grid | Price-grid bot (non-rule) | `GridConfig(lower=40000, upper=80000, levels=20, allocation_pct=50)` |
| `dca` | dca | Periodic fixed-amount accumulation (non-rule) | `DCAConfig(amount_quote=100, interval_bars=24)` |

Overridable top-level fields (`_OVERRIDABLE`,
[`templates.py:209`](../src/trader_mcp/strategy/templates.py)): `name`,
`description`, `exchange`, `symbol`, `timeframe`, `strategy_type`, `indicators`,
`entry`, `exit`, `position_sizing`, `risk`, `fees`, `grid`, `dca`.
`schema_version` is deliberately **not** overridable. An unknown override field is
rejected.

---

## 6. Worked examples

### 6.1 SMA crossover (rule strategy)

Long when a fast SMA crosses above a slow SMA; flat (and short) on the reverse.
Stop-loss 3%, take-profit 6%, 10% of equity per position.

```json
{
  "name": "sma-cross-btc",
  "description": "Long when fast SMA crosses above slow SMA; short on the reverse.",
  "exchange": "coinbase",
  "symbol": "BTC/USD",
  "timeframe": "1h",
  "strategy_type": "rule",
  "indicators": [
    { "id": "fast", "kind": "sma", "params": { "length": 10 } },
    { "id": "slow", "kind": "sma", "params": { "length": 30 } }
  ],
  "entry": {
    "long": "crossover(fast, slow)",
    "short": "crossunder(fast, slow)"
  },
  "exit": {
    "long": "crossunder(fast, slow)",
    "short": "crossover(fast, slow)"
  },
  "position_sizing": { "mode": "percent_equity", "value": 10.0 },
  "risk": { "stop_loss_pct": 3.0, "take_profit_pct": 6.0 },
  "fees": { "taker": 0.0006, "maker": 0.0002 }
}
```

The names `fast` and `slow` are the two indicators' `id`s; `crossover`/
`crossunder` are whitelisted helpers; `close` etc. are available but unused here.

### 6.2 RSI mean-reversion (rule strategy)

Buy when RSI is oversold (`< 30`), exit when it returns to the midline (`> 50`).
The short side mirrors it. 5% of equity, 2% stop / 4% target.

```json
{
  "name": "rsi-mean-reversion-btc",
  "description": "Buy oversold RSI; exit at the midline.",
  "exchange": "coinbase",
  "symbol": "BTC/USD",
  "timeframe": "1h",
  "strategy_type": "rule",
  "indicators": [
    { "id": "rsi", "kind": "rsi", "params": { "length": 14 } }
  ],
  "entry": {
    "long": "rsi < 30",
    "short": "rsi > 70"
  },
  "exit": {
    "long": "rsi > 50",
    "short": "rsi < 50"
  },
  "position_sizing": { "mode": "percent_equity", "value": 5.0 },
  "risk": { "stop_loss_pct": 2.0, "take_profit_pct": 4.0 },
  "fees": { "taker": 0.0006, "maker": 0.0002 }
}
```

Both examples are minimal variants of the `ma_cross` and `rsi_reversion`
templates — start from the template and override rather than typing them out.

---

## 7. Common validation errors

Failures surface as `{location, problem, fix}` issues (see
[`validation.py`](../src/trader_mcp/strategy/validation.py)). The most frequent:

| Symptom / message | Cause | Fix |
| --- | --- | --- |
| `unsupported timeframe '...'` | `timeframe` not on the whitelist | Use `1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w`. |
| `duplicate indicator id(s): [...]` | Two `IndicatorSpec`s share an `id` | Make every indicator `id` unique. |
| `indicator id '...' must be a valid identifier` | `id` is not a Python identifier or starts with `__` | Use letters/digits/underscore, not starting with a digit or `__`. |
| `unknown param(s) [...] for indicator kind '...'` | A param key the kind doesn't define | Use only the params listed in §4.6. |
| `param '...' must be a positive integer` | Non-positive or non-integer length-style param | Pass a positive integer. |
| `unknown name '...'` (in a rule) | Rule references a name that isn't an OHLCV column or a defined indicator output | Define the indicator (and use its output column name), or fix the typo. The error lists the available names. |
| `call to non-whitelisted function '...'` | Rule calls something other than `crossover/crossunder/abs/min/max` | Use only the helpers in §4.4. |
| `min()/max() takes exactly 2 arguments` | Used the single-arg reduce form | Use the binary element-wise form, e.g. `max(close, sma)`. |
| `attribute access ... is forbidden` / `subscripting ... is forbidden` | Used `x.y` or `x[0]` in a rule | Reference indicator outputs directly by name; use cross helpers. |
| `the expression is empty` | A rule was set to `""` | Use `null` to mean "no rule on this side". |
| `it is not valid syntax (...)` | The rule isn't a parseable expression | Write a single boolean expression, e.g. `rsi < 30 and close > sma`. |
| `strategy_type 'rule' requires at least one entry/exit rule` | A `rule` spec with all four rules `null` | Add at least one entry or exit rule. |
| `strategy_type 'grid' requires a 'grid' block` / `... must not include a 'dca' block` | Discriminator/block mismatch | Provide the block that matches `strategy_type` and omit the other. |
| `grid 'upper' must be greater than 'lower'` | Inverted grid bounds | Set `upper > lower`. |
| `percent_equity sizing value must be <= 100` | `value > 100` with `percent_equity` mode | Use `0 < value <= 100`, or switch sizing mode. |
| `Unknown override field(s) [...]` | `build_from_template` override not in `_OVERRIDABLE` | Override only the top-level fields listed in §5. |

---

## 8. See also

- [`/author-strategy`](../.claude/skills/author-strategy/SKILL.md) — guided
  authoring flow (template → fill → validate → dry-run backtest).
- [`/run-backtest`](../.claude/skills/run-backtest/SKILL.md) — run a spec through
  the one interpreter and produce a tear sheet.
- `src/trader_mcp/strategy/spec.py` — the spec models.
- `src/trader_mcp/strategy/evaluator.py` — the safe evaluator / security boundary.
- `src/trader_mcp/indicators/registry.py` — the indicator whitelist.
- `src/trader_mcp/strategy/templates.py` — the template library.
- `src/trader_mcp/strategy/validation.py` — rich validation entry points.
