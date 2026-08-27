#!/usr/bin/env python
"""Install the git hooks. Idempotent, and safe to re-run.

The hook is a thin shim: it calls scripts/docs_gate.py so there is exactly one
implementation of the policy. A hook that reimplements the checks is a second copy that
will disagree with the first.

The gate runs at **commit-msg**, not pre-commit. git writes the message file only after
the pre-commit hook has returned, so a pre-commit run reads the PREVIOUS commit's message:
the host-identifier scan and the Docs-Gate-Skip waiver parser both judged text nobody was
proposing to commit. commit-msg is the first stage where the staged index and the real
message both exist, so it is the only stage where the whole check set is answerable.

Any stale pre-commit hook from an earlier install is removed, so the two cannot disagree.
"""

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".git" / "hooks" / "commit-msg"
STALE = ROOT / ".git" / "hooks" / "pre-commit"

# "$1" is the message file git hands a commit-msg hook. Passing it is the whole point of
# running here rather than at pre-commit, so verify_installed() asserts it is present --
# a shim that silently kept an older body would reintroduce exactly the bug this fixes.
BODY = """#!/bin/sh
# Installed by scripts/install_hooks.py -- edit that, not this.
# Local feedback only. The same script runs in CI, where --no-verify cannot help.
root="$(git rev-parse --show-toplevel)"
if [ -x "$root/.venv/Scripts/python.exe" ]; then
  py="$root/.venv/Scripts/python.exe"
else
  py=python
fi
exec "$py" "$root/scripts/docs_gate.py" --mode commit-msg --message-file "$1"
"""


def verify_installed() -> None:
    """Read the hook back and confirm it is the shim we meant to write.

    Writing a file and trusting the write is how the superseded hook survived a rename:
    it was called commit-msg and still ran --mode pre-commit.
    """
    got = HOOK.read_text(encoding="utf-8")
    for needle in ("--mode commit-msg", '--message-file "$1"'):
        if needle not in got:
            raise SystemExit(f"hook verification failed: {needle!r} missing from {HOOK}")
    if "--mode pre-commit" in got:
        raise SystemExit(f"hook verification failed: {HOOK} still runs --mode pre-commit")


def main() -> int:
    if not (ROOT / ".git").is_dir():
        print("not a git working tree", file=sys.stderr)
        return 1
    HOOK.parent.mkdir(parents=True, exist_ok=True)
    if STALE.exists() and "docs_gate.py" in STALE.read_text(encoding="utf-8", errors="replace"):
        STALE.unlink()
        print(f"removed superseded {STALE.relative_to(ROOT)} (it judged the previous commit)")
    HOOK.write_text(BODY, encoding="utf-8", newline="\n")
    HOOK.chmod(HOOK.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    verify_installed()
    print(f"installed {HOOK.relative_to(ROOT)} (verified: runs --mode commit-msg)")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "docs_gate.py"),
                           "--mode", "pre-commit"], cwd=ROOT,
                          check=False)  # structural self-check only
    print(f"gate self-check exited {proc.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
