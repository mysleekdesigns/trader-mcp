---
name: next-phase
description: >-
  Orchestrate the next PRD build phase end-to-end: detect the first incomplete
  phase in PRD §6, fan out the specialized sub-agent team in parallel on
  non-overlapping slices, integrate, run the phase gate (ruff/types/pytest +
  parity), code-review, then check off the completed items in PRD.md and commit
  & push. Use when the user wants to "do/start/build the next phase".
argument-hint: "[phase-number]"
---

# Next phase — build the next PRD phase with the agent team

You are the **orchestrator**. Sub-agents run in their own context and report
back to you only; you split the work, integrate results, and resolve
cross-agent contracts. Target phase: `$ARGUMENTS` if given, else auto-detect.

**Do not skip steps 6–7 (gate + review) before 8–9 (PRD edit + push).** This
skill commits and pushes — that is authorized by its invocation, but **only
after the gate is green**. Fail closed: a red gate stops everything.

## 1. Detect the target phase
Read **PRD §6**. The target is the **first phase whose checklist still has
unchecked `[ ]` boxes** (or the explicit `$ARGUMENTS` phase). Read that phase's
**Goal**, every checklist item, and its **Exit criteria**. If all phases are
checked, report "all phases complete" and stop.

## 2. Safety gate — is this a live-money phase?
- **Phase 6+ (live trading / real money):** **STOP and ask the user.** Do not
  arm live trading, place real orders, or push live-execution code
  autonomously. Route through `risk-safety-engineer` and the `/safety-preflight`
  checklist; require explicit human authorization. This skill never arms live.
- Phases 0–5 (scaffold → connectivity → data → spec → backtest → paper/testnet)
  proceed autonomously.

## 3. Map checklist items → owning agents (stay in lane)
Assign each checklist item to the agent that owns that area (CLAUDE.md table):
`scaffolding-engineer`, `mcp-server-engineer`, `exchange-adapter-engineer`,
`data-pipeline-engineer`, `strategy-spec-engineer`, `backtest-engine-engineer`,
`risk-safety-engineer`, `qa-parity-engineer`, `quant-research-analyst`,
`code-reviewer`. Give each a **non-overlapping slice** so they don't edit the
same files. Note shared contracts (tool signatures, schemas) to resolve yourself
after they return.

## 4. Dispatch in parallel (respect the dependency order)
Launch concurrently by sending **multiple Agent calls in one message**.
- **Phase 0:** `scaffolding-engineer` **alone first** — everything depends on
  the layout/toolchain. Don't fan out until it returns.
- **Phases 1–3:** fan out `exchange-adapter`, `data-pipeline`, and
  `strategy-spec` engineers in parallel on their slices.
- **Phase 4:** `backtest-engine-engineer` (depends on spec + data being done).
- **Phase 5:** `exchange-adapter` (WS feed) + `backtest-engine` (live runtime
  via the **same interpreter**) + `risk-safety` (dry-run defaults).
- **Cross-cutting every phase:** `risk-safety-engineer` gates any execution
  path; `qa-parity-engineer` writes tests including the backtest↔live parity
  suite (Phase 4+).
- If two agents must touch overlapping files concurrently, run them with
  **`isolation: "worktree"`**; otherwise serialize edits to shared files.

In each agent's prompt, restate: the slice, **Bybit first**, the two invariants
(one interpreter for backtest+live; safe-by-default execution / specs-are-data),
typed Pydantic v2 I/O, and **secrets are radioactive** (never read/print/log
`.env` or keys).

## 5. Integrate
Wire the returned slices together: register tools/resources/prompts on the
FastMCP server, reconcile cross-agent schemas, resolve contract mismatches.
Keep the MCP SDK isolated behind the internal interface.

## 6. Run the phase gate (must be green)
Invoke `/phase-gate <N>` (or run the equivalent):
```
uv run ruff check . && uv run ruff format --check . && (uv run pyright || uv run mypy src) && uv run pytest -q
```
All must pass. Confirm: each **Exit criterion** met with evidence; **Bybit
validated first**; invariants hold; parity tests pass (Phase 4+); nothing
gitignored (`.env`, `*.duckdb`, `*.parquet`, `backtests/`, `reports/`, `logs/`)
is staged. **Any failure ⇒ do not proceed.** Loop the owning agent to fix, then
re-gate.

## 7. Code review
Run `code-reviewer` on the diff. Treat invariant violations, missing tests, or
secret leakage as **blockers**. Address findings and re-gate before landing.

## 8. Update PRD.md
Only after a green gate + clean review: check off (`[ ]` → `[x]`) the items in
the **target phase** that are genuinely done with evidence. Leave unfinished
items unchecked and say which agent owns the remainder. (Editing `PRD.md` trips
the `guard-files.py` confirmation hook — confirm the edit; do not change locked
decisions §4/§5, only the phase checkboxes.)

## 9. Commit & push
- Stage the work; double-check no secret/ignored files are included.
- Commit to the **current working branch** (do not push to `main`; if on `main`,
  branch first). Message: what the phase delivered + that the gate is green. End
  with the `Co-Authored-By:` trailer.
- Push the branch.

## Definition of done
Target phase's exit criteria met on Bybit · gate green (ruff/types/pytest +
parity) · review clean · invariants upheld · PRD §6 checkboxes updated to match
reality · committed and pushed. Report the phase completed, what's checked off,
and any items deferred to a follow-up.
