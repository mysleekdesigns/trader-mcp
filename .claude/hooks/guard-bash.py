#!/usr/bin/env python3
"""PreToolUse guard for the Bash tool.

Denies shell commands that would print, read, or stage secrets (.env, keys),
enforcing the trader-mcp rule: never read, print, or commit .env contents.
Targeted patterns only, to avoid blocking normal dev commands (e.g. `uv run`,
`env FOO=bar cmd`, `cp .env.example .env`). Stdlib only.
"""
import json
import re
import sys

# Non-secret template variants that are safe to read/commit.
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")

# Read/print of a .env-style file via a viewing/printing utility.
READ_ENV = re.compile(
    r"(?:^|[\s;&|()`$])"
    r"(?:cat|bat|less|more|head|tail|nl|xxd|od|hexdump|strings|grep|rg|source|\.)"
    r"\s+[^\n;|&]*\.env(?:\.[\w-]+)?(?:$|[\s;|&)])"
)
# Read/print of a private key / cert.
READ_KEY = re.compile(
    r"(?:^|[\s;&|()`$])"
    r"(?:cat|bat|less|more|head|tail|strings|grep|rg)"
    r"\s+[^\n;|&]*\.(?:pem|key|p12|pfx)(?:$|[\s;|&)])"
)
# `printenv` (dumps env) or bare `env` / `env | ...` (dumps env).
DUMP_ENV = re.compile(r"(?:^|[\s;&|()`$])(?:printenv\b|env\s*(?:$|\|))")
# Staging a .env into git.
GIT_ADD = re.compile(r"git\s+add\b[^\n;|&]*\.env")
# Any .env reference, to classify secret vs template.
ENV_REF = re.compile(r"\.env(\.[\w-]+)?")


def references_secret_env(cmd):
    """True if cmd names a real secret .env file (not a *.example template)."""
    for m in ENV_REF.finditer(cmd):
        suffix = (m.group(1) or "").lower()
        if suffix in SAFE_ENV_SUFFIXES:
            continue
        return True
    return False


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cmd = (data.get("tool_input", {}) or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd:
        sys.exit(0)

    if DUMP_ENV.search(cmd) or READ_KEY.search(cmd):
        deny("Blocked: command would print secrets (env dump or key file). "
             "trader-mcp never prints credentials; reference them via the "
             "process environment or keyring instead of dumping them.")
    if READ_ENV.search(cmd) and references_secret_env(cmd):
        deny("Blocked: command would read a .env secret file. trader-mcp never "
             "reads or prints .env contents. (Template files like .env.example "
             "are allowed.)")
    if GIT_ADD.search(cmd) and references_secret_env(cmd):
        deny("Blocked: refusing to stage a .env secret file. Secrets must stay "
             "gitignored and out of version control.")

    sys.exit(0)


if __name__ == "__main__":
    main()
