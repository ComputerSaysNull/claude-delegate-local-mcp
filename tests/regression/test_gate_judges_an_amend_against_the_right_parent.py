"""An amend is judged against the parent it will actually have.

The owning-doc check compared the staged index against HEAD. That is right for a normal
commit and wrong for `git commit --amend`, whose parent is HEAD~1: the files already inside
the commit being amended are part of what lands but are absent from the index, so a
complete commit was reported as incomplete. The documented workaround was to undo the
commit and remake it -- a lot of ceremony to answer a question the tool got wrong.

It cannot simply be detected. `git commit --amend` and `git commit -C HEAD` reach a hook
identically: source="commit", sha="HEAD", GIT_REFLOG_ACTION unset. Measured, not assumed.
So both readings are evaluated, strict first, and a pass that depended on the amend reading
is announced rather than taken quietly.

These tests assert the block still fires where it should. A change that merely stopped
blocking amends would satisfy a weaker suite and disarm the check.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

MANIFEST = '''[docs."docs/THING.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/pkg/**"]
covers_not = "nothing"

[unowned]
paths = ["scripts/**", "security/**"]
'''

GATE_EMAIL = "noreply@" + "github.com"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           *args], cwd=repo, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    for d in ("scripts", "security", "docs", "src/pkg"):
        (r / d).mkdir(parents=True, exist_ok=True)
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(MANIFEST, encoding="utf-8")
    (r / "docs" / "THING.md").write_text("<!-- BUDGET: 50 -->\n# Thing\n", encoding="utf-8")
    (r / "src" / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    for name in ("allowed_emails.txt", "content_safe_emails.txt"):
        (r / "security" / name).write_text(
            "t@example.com\n" + GATE_EMAIL + "\n", encoding="utf-8")
    (r / "security" / "secret_globs.txt").write_text(".env\n", encoding="utf-8")
    (r / "security" / "forbidden_strings.txt").write_text("", encoding="utf-8")
    (r / ".gitignore").write_text("security/forbidden_strings.txt\n", encoding="utf-8")
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


def run_gate(repo: Path, msg: str, *, reused: bool) -> subprocess.CompletedProcess:
    """Drive commit-msg mode, planting the marker the prepare-commit-msg hook would write."""
    f = repo / ".git" / "COMMIT_MSG_UNDER_TEST"
    f.write_text(msg, encoding="utf-8")
    marker = repo / ".git" / "docs-gate-reused-message"
    if reused:
        marker.write_text("", encoding="utf-8")
    elif marker.exists():
        marker.unlink()
    return subprocess.run([sys.executable, "scripts/docs_gate.py", "--mode", "commit-msg",
                           "--message-file", str(f)],
                          cwd=repo, capture_output=True, text=True, check=False)


def _commit_code_and_doc_together(repo: Path) -> None:
    (repo / "src" / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "docs" / "THING.md").write_text(
        "<!-- BUDGET: 50 -->\n# Thing\n\nupdated\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: code and its document together")


def test_an_amend_that_is_complete_is_allowed(repo: Path):
    """The bug. Code and doc landed together; amending must not report them missing."""
    _commit_code_and_doc_together(repo)
    (repo / "src" / "pkg" / "a.py").write_text("x = 3\n", encoding="utf-8")
    git(repo, "add", "-A")

    out = run_gate(repo, "feat: code and its document together\n", reused=True)
    assert out.returncode == 0, f"a complete amend was blocked:\n{out.stdout}"


def test_the_pass_says_it_depended_on_the_amend_reading(repo: Path):
    """It must not be silent: -C HEAD is indistinguishable and would be wrong."""
    _commit_code_and_doc_together(repo)
    (repo / "src" / "pkg" / "a.py").write_text("x = 3\n", encoding="utf-8")
    git(repo, "add", "-A")

    out = run_gate(repo, "feat: code and its document together\n", reused=True)
    assert "read as an amend" in out.stdout, f"the relaxation was silent:\n{out.stdout}"
    assert "docs/THING.md" in out.stdout, "it did not name what it counted"


def test_an_amend_still_blocks_when_the_document_is_nowhere(repo: Path):
    """The check must keep firing. Code changed, doc untouched in either commit."""
    (repo / "src" / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: code alone")   # already a violation
    (repo / "src" / "pkg" / "a.py").write_text("x = 3\n", encoding="utf-8")
    git(repo, "add", "-A")

    out = run_gate(repo, "feat: code alone\n", reused=True)
    assert out.returncode == 1, f"an incomplete amend was allowed:\n{out.stdout}"
    assert "owning-doc" in out.stdout


def test_an_ordinary_commit_is_unaffected(repo: Path):
    """Without the marker, nothing widens -- code without its doc still blocks."""
    (repo / "src" / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
    git(repo, "add", "-A")

    out = run_gate(repo, "feat: code alone\n", reused=False)
    assert out.returncode == 1, f"an ordinary incomplete commit passed:\n{out.stdout}"


def test_a_stale_marker_cannot_change_the_next_commit(repo: Path):
    """The marker is consumed on read, so one left by an aborted commit cannot linger."""
    _commit_code_and_doc_together(repo)
    (repo / "src" / "pkg" / "a.py").write_text("x = 3\n", encoding="utf-8")
    git(repo, "add", "-A")
    marker = repo / ".git" / "docs-gate-reused-message"

    # A conventional subject: the gate now checks shape too, and this test is about the
    # marker's lifetime rather than the message.
    first = run_gate(repo, "fix: msg\n", reused=True)
    assert first.returncode == 0, first.stdout
    assert not marker.exists(), "the marker survived a run and would judge the next commit"

    second = run_gate(repo, "fix: msg\n", reused=False)
    assert second.returncode == 1, (
        f"the second run still behaved as an amend:\n{second.stdout}")
