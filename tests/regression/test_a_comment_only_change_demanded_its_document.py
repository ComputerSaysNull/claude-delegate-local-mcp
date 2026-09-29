"""A comment-only edit to an owned module no longer demands its document.

`check_ownership` sees file names only, so reworking a comment in an owned `src/` file
blocked on its owning document exactly as a behaviour change does -- 14 waivers in three
days, 11 of them comment passes. The exemption is judged by token stream, comment tokens
dropped, so a docstring reword or a code change still blocks, and a non-Python file or a
file that no longer tokenizes is never exempt.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

MANIFEST = '''[docs."docs/OWNER.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/pkg/mod.py", ".github/workflows/x.yml"]
covers_not = "nothing"

[unowned]
paths = ["scripts/**", "security/**"]
'''

MOD = '''# the answer
# the plan
def f():
    """Return one."""
    return 1
'''

MOD_COMMENT_ONLY = '''# the answer reworded
# a third note
def f():
    """Return one."""
    return 1
'''

MOD_DOCSTRING = '''# the answer
# the plan
def f():
    """Return two."""
    return 1
'''

MOD_CODE = '''# the answer
# the plan
def f():
    """Return one."""
    return 2
'''

MOD_COMMENT_AND_CODE = '''# the answer reworded
# the plan
def f():
    """Return one."""
    return 2
'''

# A comment reworded plus an unclosed bracket: the file no longer tokenizes, so it is a
# behaviour change rather than comment-only even though the only code edit is the bracket.
MOD_UNTOKENIZABLE = '''# the answer reworded
# the plan
def f():
    """Return one."""
    return (1
'''

X_YML = '''# the build
on: push
jobs:
  build:
    runs-on: ubuntu
    steps:
      - run: echo hi
'''

X_YML_CHANGED = '''# the build step
on: push
jobs:
  build:
    runs-on: ubuntu
    steps:
      - run: echo hi
'''


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           *args], cwd=repo, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    for d in ("scripts", "security", "docs", "src/pkg", ".github/workflows"):
        (r / d).mkdir(parents=True, exist_ok=True)
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(MANIFEST, encoding="utf-8")
    (r / "docs" / "OWNER.md").write_text("<!-- BUDGET: 50 -->\n# Owner\n", encoding="utf-8")
    (r / "src" / "pkg" / "mod.py").write_text(MOD, encoding="utf-8")
    (r / ".github" / "workflows" / "x.yml").write_text(X_YML, encoding="utf-8")
    for name in ("allowed_emails.txt", "content_safe_emails.txt"):
        (r / "security" / name).write_text(
            "t@example.com\n" + "noreply@" + "github.com" + "\n", encoding="utf-8")
    (r / "security" / "secret_globs.txt").write_text(".env\n", encoding="utf-8")
    (r / "security" / "forbidden_strings.txt").write_text("", encoding="utf-8")
    (r / ".gitignore").write_text("security/forbidden_strings.txt\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    # Repo-local identity, so the gate's own `git config user.email` reads an allowlisted
    # address rather than whatever global identity the machine happens to carry.
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


def gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/docs_gate.py", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


def precommit(repo: Path) -> subprocess.CompletedProcess:
    """Stage everything, then run the gate as a pre-commit hook would."""
    git(repo, "add", "-A")
    return gate(repo, "--mode", "pre-commit")


def test_a_comment_only_change_is_not_held_to_its_document(repo: Path):
    """The bug. Comment reworded, one added, one removed -- the gate must not demand OWNER.md."""
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_COMMENT_ONLY, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 0, out.stdout
    assert "owning-doc" in out.stdout
    assert "changed only in comments" in out.stdout
    assert "src/pkg/mod.py" in out.stdout


def test_a_docstring_reword_is_still_held_to_its_document(repo: Path):
    """A docstring is a STRING token, so it is a behaviour change, not comment text."""
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_DOCSTRING, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 1, out.stdout
    assert "owning-doc" in out.stdout


def test_a_code_change_is_still_held_to_its_document(repo: Path):
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_CODE, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 1, out.stdout
    assert "owning-doc" in out.stdout


def test_a_comment_and_a_code_change_is_still_held_to_its_document(repo: Path):
    """The exemption must not swallow a real change hiding beside a comment edit."""
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_COMMENT_AND_CODE, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 1, out.stdout
    assert "owning-doc" in out.stdout


def test_a_comment_only_change_to_a_non_python_file_is_still_held(repo: Path):
    """Only `.py` files can be comment-only; a YAML `#` edit is a behaviour change."""
    (repo / ".github" / "workflows" / "x.yml").write_text(X_YML_CHANGED, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 1, out.stdout
    assert "owning-doc" in out.stdout


def test_a_file_that_no_longer_tokenizes_is_still_held(repo: Path):
    """A file that cannot be read as tokens is a behaviour change by default."""
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_UNTOKENIZABLE, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 1, out.stdout
    assert "owning-doc" in out.stdout


def test_ci_holds_a_comment_only_change_to_nothing(repo: Path):
    """The CI reading must apply the same exemption, from its own range's base."""
    git(repo, "checkout", "-q", "-b", "topic")
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_COMMENT_ONLY, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: comment-only tweak")
    out = gate(repo, "--mode", "ci", "--diff", "HEAD~1...HEAD")
    assert out.returncode == 0, out.stdout
    assert "owning-doc" in out.stdout
    assert "changed only in comments" in out.stdout
    assert "src/pkg/mod.py" in out.stdout
