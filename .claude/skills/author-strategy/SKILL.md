---
name: author-strategy
description: >-
  Guided flow to author a declarative trader-mcp strategy spec from a template:
  pick a template, fill typed Pydantic v2 fields, validate, confirm every rule
  uses only whitelisted indicators/operators, then dry-run backtest it. Use when
  the user wants to create, edit, or validate a strategy.
argument-hint: "[strategy-name]"
---

# Author a strategy spec (trader-mcp)

Strategy name (if given): `$ARGUMENTS`. Strategies are **declarative data, never
code** — never write or eval arbitrary Python for strategy logic.

## 1. Start from a template
Pick the closest template from the strategy template library (e.g. MA-cross,
breakout, RSI mean-reversion) rather than authoring from scratch.

## 2. Fill the typed spec (Pydantic v2)
Set: instrument(s)/timeframe, entry rules, exit rules, position sizing, and risk
limits. Every field is typed and validated. Keep it **execution-agnostic** — the
same spec must run unchanged in backtest and live (the one-interpreter invariant).

## 3. Stay inside the whitelist (security boundary)
Every indicator and operator used in a rule expression MUST be on the evaluator
whitelist. If you need something new, add it to the indicator registry via
`strategy-spec-engineer` (with tests) — do **not** smuggle logic through free-form
expressions, `eval`, or lambdas.

## 4. Validate
Load the spec through its Pydantic model and the safe evaluator. Fix validation
errors. Confirm warmup/lookback for each indicator is satisfied by the data range.

## 5. Set risk limits
Sizing and per-strategy risk limits are required before any (later) live use.
Coordinate defaults with `risk-safety-engineer`. Leave the spec **dry-run** —
authoring never arms live trading.

## 6. Dry-run backtest
Use the `/run-backtest` flow on real cached data to sanity-check signals and the
tear sheet before saving the spec as an MCP resource.

## Definition of done
A spec that validates, uses only whitelisted constructs, has risk limits set,
and produces a sane dry-run backtest.
