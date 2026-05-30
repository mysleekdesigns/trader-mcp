---
name: safety-preflight
description: >-
  Safety checklist to run before merging ANY change that touches an execution
  path, arming, risk limits, credentials, or logging near secrets in trader-mcp.
  Use proactively when editing order placement, deploy_strategy, arm_live_trading,
  kill_switch, key handling, or anything under the safety module.
---

# Safety preflight (trader-mcp)

Run this before approving/merging any execution-, arming-, risk-, or
secret-touching change. **Fail closed** — if a box can't be checked, block.

## Execution defaults
- [ ] `place_order` / `deploy_strategy` are **dry-run by default**; the `dry_run`
      flag defaults to `True`.
- [ ] No code path places a **real** order without passing the `arm_live_trading`
      gate **and** per-strategy risk limits.
- [ ] Real-money live trading is not enabled before **Phase 6**.

## Arming & limits
- [ ] Arming is explicit, auditable, and revocable; `kill_switch` halts all live
      activity (and cancels/flattens per config).
- [ ] Risk limits are enforced server-side, not just advisory.

## Keys & secrets
- [ ] Keys are scoped **read-only vs trade-enabled**; a read-only credential is
      structurally unable to trade.
- [ ] Secrets come from env/`.env`/keyring only; they are **never** logged,
      printed, or returned in tool output. No `.env` contents are read or echoed.

## Specs-are-data
- [ ] No `eval`/`exec`/dynamic import or arbitrary callables in the strategy
      evaluator — whitelisted indicators/operators only.

## Tests
- [ ] Tests prove: dry-run default, unarmed cannot place real orders, read-only
      keys cannot trade, limits enforced, secrets absent from logs/outputs.

If anything here is uncertain, route to `risk-safety-engineer` and **do not
merge**.
