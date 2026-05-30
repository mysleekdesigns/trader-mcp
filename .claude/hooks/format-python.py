#!/usr/bin/env python3
"""PostToolUse hook: auto-format edited Python files with ruff.

Runs `uv run ruff format` then `uv run ruff check --fix` on the file that was
just written/edited. Non-blocking by design: any problem (no toolchain yet,
ruff not installed, lint errors) is swallowed and the hook always exits 0, so it
never interrupts the agent. Becomes active once Phase 0 scaffolding (pyproject +
ruff) exists. Stdlib only.
"""
import json
import os
import shutil
import subprocess
import sys


def find_uv():
    uv = shutil.which("uv")
    if uv:
        return uv
    for cand in (os.path.expanduser("~/.local/bin/uv"),
                 os.path.expanduser("~/.cargo/bin/uv")):
        if os.path.exists(cand):
            return cand
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    fp = (data.get("tool_input", {}) or {}).get("file_path", "")
    if not isinstance(fp, str) or not fp.endswith(".py") or not os.path.exists(fp):
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not os.path.exists(os.path.join(project_dir, "pyproject.toml")):
        sys.exit(0)  # Phase 0 not done yet; nothing to format against.

    uv = find_uv()
    if not uv:
        sys.exit(0)

    for args in (["run", "ruff", "format", fp],
                 ["run", "ruff", "check", "--fix", fp]):
        try:
            subprocess.run([uv] + args, cwd=project_dir, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
