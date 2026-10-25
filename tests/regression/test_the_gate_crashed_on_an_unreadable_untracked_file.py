"""The gate crashed on a file it cannot read that git does not even track.

Claude Code's sandbox protects paths such as `.claude/loop.md` by binding `/dev/null` over
them, creating an empty placeholder first when the path does not exist. Inside a sandboxed
command that placeholder is unreadable, and two checks -- the body-history check over
`.claude/` and the budget check over every `*.md` -- listed files with `rglob` rather than
`git ls-files`. So `--mode pre-commit` died with a `PermissionError` on a file that was
never going to be committed, and no commit could be made from the sandbox at all. Every
other check in the gate already lists tracked files only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_gate_checks_can_fail import repo  # noqa: F401 -- the fixture

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a mode-000 file anyway")


def run_gate(repo: Path) -> subprocess.CompletedProcess[str]:  # noqa: F811
    """The gate as the commit hook runs it, without `git add -A`: the file stays untracked."""
    return subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


@pytest.mark.parametrize("rel", [".claude/loop.md", "docs/placeholder.md"])
def test_an_unreadable_untracked_markdown_file_does_not_crash_the_gate(repo: Path, rel: str):  # noqa: F811
    placeholder = repo / rel
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    placeholder.write_text("", encoding="utf-8")
    placeholder.chmod(0)
    try:
        proc = run_gate(repo)
    finally:
        placeholder.chmod(0o644)
    assert "Traceback" not in proc.stderr, proc.stderr[-800:]
    assert "PermissionError" not in proc.stderr


def test_a_tracked_document_over_budget_is_still_checked(repo: Path):  # noqa: F811
    """The other direction: listing tracked files must not drop the files that matter."""
    (repo / "docs" / "big.md").write_text("<!-- BUDGET: 5 -->\n" + "line\n" * 40,
                                          encoding="utf-8")
    subprocess.run(["git", "add", "docs/big.md"], cwd=repo, capture_output=True)
    proc = run_gate(repo)
    assert any("[budget]" in ln and "docs/big.md" in ln for ln in proc.stdout.splitlines())
