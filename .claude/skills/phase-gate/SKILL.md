---
name: phase-gate
description: >-
  Verify a PRD build-phase's exit criteria before moving to the next phase: runs
  ruff, type-check, and pytest, and walks the per-phase checklist from PRD §6.
  Deliberate, user-invoked gate.
disable-model-invocation: true
argument-hint: "[phase-number]"
---

# Phase gate — verify exit criteria before advancing

Checking exit criteria for **Phase $ARGUMENTS** (PRD §6). Do not advance to the
next phase until every box is green.

## Automated checks
```!
uv run ruff check . && echo "ruff: OK"
uv run ruff format --check . && echo "format: OK"
uv run pyright || uv run mypy src
uv run pytest -q
```
If any command fails (or the toolchain isn't installed yet because Phase 0 is
incomplete), that is a gate failure — fix before proceeding.

## Per-phase checklist (PRD §6)
1. Open `PRD.md` and read the exit criteria for **Phase $ARGUMENTS**.
2. Confirm each criterion is met with evidence (a passing test, a working
   command, a generated artifact).
3. **Bybit is validated first** for the phase's functionality before fanning out
   to BloFin/Toobit/WeeX.
4. Confirm the architectural invariants still hold: one interpreter for
   backtest+live, safe-by-default execution, specs-are-data, typed MCP I/O.
5. Confirm nothing that should be gitignored (`.env`, `*.duckdb`, `*.parquet`,
   `backtests/`, `reports/`, `logs/`) is staged.

## Output
A short PASS/FAIL per criterion and an overall verdict. On FAIL, list exactly
what's missing and which agent owns the fix.
