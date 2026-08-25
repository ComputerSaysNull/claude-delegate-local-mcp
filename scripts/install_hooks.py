#!/usr/bin/env python
"""Install the git hooks. Idempotent, and safe to re-run.

The hook is a thin shim: it calls scripts/docs_gate.py so there is exactly one
implementation of the policy. A hook that reimplements the checks is a second copy that
will disagree with the first.
"""

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".git" / "hooks" / "pre-commit"

BODY = """#!/bin/sh
# Installed by scripts/install_hooks.py -- edit that, not this.
# Fast local feedback only. The same script runs in CI, where --no-verify cannot help.
exec "$(git rev-parse --show-toplevel)/.venv/Scripts/python.exe" \
     "$(git rev-parse --show-toplevel)/scripts/docs_gate.py" --mode pre-commit 2>/dev/null \
  || exec python "$(git rev-parse --show-toplevel)/scripts/docs_gate.py" --mode pre-commit
"""


def main() -> int:
    if not (ROOT / ".git").is_dir():
        print("not a git working tree", file=sys.stderr)
        return 1
    HOOK.parent.mkdir(parents=True, exist_ok=True)
    HOOK.write_text(BODY, encoding="utf-8", newline="\n")
    HOOK.chmod(HOOK.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {HOOK.relative_to(ROOT)}")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "docs_gate.py"),
                           "--mode", "pre-commit"], cwd=ROOT)
    print(f"gate self-check exited {proc.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
