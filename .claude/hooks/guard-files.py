#!/usr/bin/env python3
"""PreToolUse guard for file tools (Read/Edit/Write/MultiEdit/Notebook*).

Enforces trader-mcp invariants:
  - Never read secrets (.env, keys, /secrets/, ~/.ssh) -> deny.
  - Confirm before writing/editing secret files -> ask.
  - Confirm before editing PRD.md (the locked source of truth) -> ask.

Reads the hook JSON from stdin; prints a PreToolUse decision to stdout when it
wants to deny/ask, otherwise exits 0 (normal permission flow). Stdlib only.
"""
import json
import os
import sys


def decision(kind, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,          # "deny" or "ask"
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = data.get("tool_name", "") or ""
    tin = data.get("tool_input", {}) or {}

    paths = []
    for key in ("file_path", "notebook_path", "path"):
        val = tin.get(key)
        if isinstance(val, str) and val:
            paths.append(val)
    if not paths:
        sys.exit(0)

    is_read = tool in ("Read", "NotebookRead")
    is_write = tool in ("Edit", "Write", "MultiEdit", "NotebookEdit")

    for p in paths:
        norm = p.replace("\\", "/")
        base = os.path.basename(norm.rstrip("/"))
        low = norm.lower()

        safe_env = base in (".env.example", ".env.sample", ".env.template",
                            ".env.dist", ".env.defaults")
        secret = (
            base == ".env"
            or (base.startswith(".env.") and not safe_env)
            or base in (".envrc", "credentials", "credentials.json", ".netrc")
            or base.endswith((".pem", ".key", ".p12", ".pfx"))
            or "id_rsa" in base
            or "id_ed25519" in base
            or "/secrets/" in low
            or "/.ssh/" in low
            or "/.aws/" in low
        )
        if secret:
            if is_read:
                decision("deny",
                         "Blocked: %s holds secrets. trader-mcp never reads or "
                         "prints credentials; load them from env/keyring at "
                         "runtime instead." % base)
            if is_write:
                decision("ask",
                         "Confirm: writing to secret file %s. Make sure no "
                         "secret values are committed or logged." % base)

        if is_write and base == "PRD.md":
            decision("ask",
                     "PRD.md is the locked source of truth (PRD §4/§5 decisions "
                     "are not to be re-litigated). Confirm this edit is intended.")

    sys.exit(0)


if __name__ == "__main__":
    main()
