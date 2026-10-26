"""A pin refresh to `.github/ci-constraints.txt` no longer demands CONTRIBUTING.md.

The CI constraints file sits under `.github/**`, which CONTRIBUTING.md owns, so every
pin refresh tripped the owning-document check even though it changes no fact CONTRIBUTING
states. The fix declares the file unowned, and makes `--owner` resolve `[unowned]` before
looking up owners, matching `check_ownership` -- which already reads unowned first. Before
the fix a path matching both an owns glob and `[unowned]` was skipped by the gate but still
named its document by `--owner`.

Named after the bug, per the project's convention.
"""

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
owns = [".github/**", "src/pkg/mod.py"]
covers_not = "nothing"

[unowned]
paths = [".github/pins.txt", "scripts/**", "security/**"]
'''

PINS = "fastmcp==4.1.4\nhttpx==0.28.1\n"

PINS_CHANGED = "fastmcp==4.1.5\nhttpx==0.28.1\n"

MOD = '''# the answer
# the plan
def f():
    """Return one."""
    return 1
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
    (r / ".github" / "pins.txt").write_text(PINS, encoding="utf-8")
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


def test_a_the_real_pin_file_is_declared_unowned():
    """The real manifest: `--owner` must call the CI constraints file unowned."""
    out = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--owner", ".github/ci-constraints.txt"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout
    assert "declared unowned" in out.stdout
    assert "ci-constraints.txt" in out.stdout


def test_b_a_pin_refresh_is_not_held_to_its_document(repo: Path):
    """A commit staging only the pins file must pass -- it is declared unowned."""
    (repo / ".github" / "pins.txt").write_text(PINS_CHANGED, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 0, out.stdout
    assert "owning-doc" not in out.stdout


def test_c_a_real_change_to_owned_workflow_code_is_still_held(repo: Path):
    """Negative control: a `.github` change that is NOT the pins file still blocks."""
    (repo / ".github" / "workflows" / "x.yml").write_text(X_YML_CHANGED, encoding="utf-8")
    out = precommit(repo)
    assert out.returncode == 1, out.stdout
    assert "owning-doc" in out.stdout
    assert "OWNER.md" in out.stdout


def test_d_owner_resolves_unowned_before_the_owns_glob(repo: Path):
    """`--owner` must use the gate's own order: unowned first, then owners."""
    out = gate(repo, "--owner", ".github/pins.txt")
    assert out.returncode == 0, out.stdout
    assert "declared unowned" in out.stdout
    assert "OWNER.md" not in out.stdout
