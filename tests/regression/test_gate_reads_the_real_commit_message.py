"""The gate must judge the message being written, not the one already committed.

The bug: in pre-commit mode both message-dependent checks read `.git/COMMIT_EDITMSG`.
git writes that file *after* the pre-commit hook returns, so both read the PREVIOUS
commit's message.

Two consequences, and the second is the worse one:

  - the host-identifier scan passed on text nobody was proposing to commit, while the
    message actually being written went unscanned;
  - the `Docs-Gate-Skip` waiver parser applied the previous commit's waiver to this one,
    so an escape hatch fired on a commit that never asked for it.

The fix moves both to a commit-msg hook, which git hands the real message file. These
tests assert the checks FIRE, not merely that a clean case passes -- the old code passed
every clean case too, which is precisely why nobody noticed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

# Assembled at runtime so this source file contains no string the gate would match. The
# tests still feed the complete value to the gate, so the real pattern is exercised -- but
# a test proving the scanner works must not itself trip the scanner, and exempting the file
# would be a hole rather than a fix. Same convention as test_forbidden_matching.py.
PLANTED = "10.11." + "12.13:8888"

# docs_gate.py is copied into the fixture wholesale and its own source names this address,
# so the fixture has to allowlist it. Split for the same reason.
GATE_EMAIL = "noreply@" + "github.com"

MANIFEST = '''[docs."docs/THING.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/pkg/**"]
covers_not = "nothing"

[unowned]
paths = ["scripts/**", "security/**"]
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repository the gate can run against, with one owned source file."""
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    (r / "src" / "pkg").mkdir(parents=True)

    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(MANIFEST, encoding="utf-8")
    (r / "docs" / "THING.md").write_text("<!-- BUDGET: 50 -->\n# Thing\n", encoding="utf-8")
    (r / "src" / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    # docs_gate.py is copied in wholesale, and its own source mentions this address.
    # The email check scans every tracked file, not just changed ones.
    for name in ("allowed_emails.txt", "content_safe_emails.txt"):
        (r / "security" / name).write_text(
            "t@example.com\n" + GATE_EMAIL + "\n", encoding="utf-8")
    (r / "security" / "secret_globs.txt").write_text(".env\n", encoding="utf-8")
    # Must exist for the scan to run, and must NOT be tracked -- the never-track check
    # blocks on it by design, which is the behaviour the real repo depends on.
    (r / "security" / "forbidden_strings.txt").write_text("", encoding="utf-8")
    (r / ".gitignore").write_text("security/forbidden_strings.txt\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-q", "-m", "initial"], cwd=r, capture_output=True)
    return r


def run_gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/docs_gate.py", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


def write_msg(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "COMMIT_MSG"
    f.write_text(body, encoding="utf-8")
    return f


def test_commit_msg_mode_blocks_an_address_in_the_message(repo: Path, tmp_path: Path):
    msg = write_msg(tmp_path, f"fix: a thing\n\nreached {PLANTED} while debugging\n")
    out = run_gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert out.returncode == 1, f"the gate did not block:\n{out.stdout}{out.stderr}"
    assert "commit-message" in out.stdout


def test_the_same_message_without_the_specimen_passes(repo: Path, tmp_path: Path):
    """Guards against a check that blocks everything, which is equally useless."""
    msg = write_msg(tmp_path, "fix: a thing\n\nreached the head node on its port\n")
    out = run_gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert out.returncode == 0, f"a clean message was blocked:\n{out.stdout}{out.stderr}"


def _findings(stdout: str, check: str) -> list[str]:
    """Levels reported for one check.

    Substring matching on the whole output is not good enough here: an unrelated SKIP
    elsewhere plus a BLOCK for this check satisfies `"SKIP" in out and check in out`.
    An earlier draft of these tests did exactly that and passed against the bug.
    """
    return [ln.split()[0] for ln in stdout.splitlines()
            if f"[{check}]" in ln and ln.split()]


def test_pre_commit_does_not_pretend_to_have_scanned_the_message(repo: Path):
    """It must skip, and specifically must not report a verdict on the stale file."""
    (repo / ".git" / "COMMIT_EDITMSG").write_text(
        f"chore: previous\n\n{PLANTED}\n", encoding="utf-8")
    out = run_gate(repo, "--mode", "pre-commit")
    levels = _findings(out.stdout, "commit-message")
    assert levels == ["SKIP"], (
        f"expected exactly one SKIP for commit-message, got {levels}:\n{out.stdout}")


def test_a_stale_waiver_does_not_carry_into_the_next_commit(repo: Path):
    """The previous commit's Docs-Gate-Skip must not waive a real block here.

    A waiver only shows itself when there is something to waive, so this stages an
    owning-doc violation on purpose: source under src/pkg/ changed with docs/THING.md
    left alone. Without that, the assertion holds vacuously.
    """
    (repo / "src" / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    (repo / ".git" / "COMMIT_EDITMSG").write_text(
        "chore: previous\n\nDocs-Gate-Skip: owning-doc -- reason from another commit\n",
        encoding="utf-8")

    out = run_gate(repo, "--mode", "pre-commit")
    assert "WAIVED" not in out.stdout, f"a stale waiver was applied:\n{out.stdout}"
    assert out.returncode == 1, (
        f"the owning-doc violation was not blocked, so the waiver silently took "
        f"effect:\n{out.stdout}")


def test_commit_msg_mode_still_honours_a_waiver_in_the_real_message(repo: Path, tmp_path: Path):
    """The escape hatch must keep working where the message is genuinely present."""
    msg = write_msg(tmp_path, "chore: thing\n\nDocs-Gate-Skip: owning-doc -- deliberate\n")
    out = run_gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert out.returncode == 0, f"{out.stdout}{out.stderr}"


def test_commit_msg_mode_requires_the_message_file(repo: Path):
    out = run_gate(repo, "--mode", "commit-msg")
    assert out.returncode == 1
    assert "requires --message-file" in out.stdout
