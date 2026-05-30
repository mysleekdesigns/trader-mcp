---
name: add-mcp-tool
description: >-
  Checklist and procedure for adding or changing a trader-mcp MCP tool the
  correct, safe way. Use when adding a FastMCP tool, exposing a resource/prompt,
  or wiring an execution tool. Ensures typed Pydantic v2 input, typed output /
  outputSchema, dry-run defaults for execution, and tests.
argument-hint: "[tool-name]"
---

# Add an MCP tool (trader-mcp)

Follow this every time you add or change a tool. Tool name (if given): `$ARGUMENTS`.

## 1. Decide the group (PRD §5.2)
connectivity/admin · market data & discovery · historical data sync · strategy
authoring · backtest & optimize · paper/testnet execution · portfolio/analytics ·
safety. Put the tool with its peers.

## 2. Define typed I/O (non-negotiable)
- **Input:** a Pydantic v2 model — every field typed, validated, with a clear
  description. No bare/dict args.
- **Output:** a Pydantic v2 model surfaced as **`outputSchema`** (structured,
  typed). Errors are typed too — never raise raw strings to the client.

## 3. Honor the invariants
- **Safe-by-default:** if the tool can place/route/deploy orders, it is
  **dry-run unless armed**. Add the `arm_live_trading` + risk-limit gate
  (consult `risk-safety-engineer`). Default the `dry_run` field to `True`.
- **SDK isolation:** register through the internal MCP interface; do not import
  the `mcp` SDK directly in domain modules.
- **Secrets:** never accept secrets as tool args or echo them in output; pull
  from env/`.env`/keyring.

## 4. Register & document
- Register the tool/resource/prompt with the FastMCP server.
- If it produces a dataset, saved spec, or report, also expose it as an MCP
  **resource**.
- Add a one-line docstring users will read in the client.

## 5. Test (hand to `qa-parity-engineer` or do inline)
- Unit test: valid input → typed output; invalid input → validation error.
- For execution tools: a test proving the **default path does not place a real
  order**.
- Keep tests deterministic — no live network (use fixtures/cassettes).

## 6. Verify
Run `uv run ruff check .`, `uv run pyright`, and `uv run pytest` for the touched
area before calling it done.

## Definition of done
Typed input + typed output/`outputSchema` + registered + (resource if it emits
artifacts) + dry-run default for execution + passing tests + clean lint/types.
