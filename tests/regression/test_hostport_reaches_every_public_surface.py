"""A `host:port` was blocked in a file and waved through everywhere it actually matters.

The bug: `HOSTPORT_RE` was applied in exactly one place, the tracked-file scan. The commit
message and the pull request title and body -- the surfaces that reach strangers, and the
one that cannot be recalled once published -- got a different, older scan that knew about
RFC1918 addresses and private-DNS suffixes but not about a named host with a port on it.

So `docs/X.md` containing a real host and port was blocked, while a commit message saying
the same thing passed, and a pull request body saying it passed too. `scan_text` had
claimed in its own docstring to be the implementation for all of them since before it was
one; `check_commit_message` carried a near-copy that had drifted.

Found by running the gate over a pull request body about to be published and checking that
the check could fail, rather than reading it and concluding that it worked.

These tests assert the block FIRES on every surface. The old code passed every clean case
on all three, which is exactly why nothing noticed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

# Assembled at runtime so this source file contains no string the gate would match, exactly
# as test_gate_reads_the_real_commit_message.py does. The tests still hand the gate the
# whole value, so the real pattern is exercised -- a test proving the scanner works must
# not itself trip the scanner, and exempting the file would be a hole rather than a fix.
PLANTED = "some-real-host" + ":8000"

# Allowed by name, and asserted separately so the fix cannot be "block every host:port".
PLACEHOLDERS = ("example.com:8000", "your-host:8000", "localhost:8765")

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
    """A minimal repository the gate can run against, never the real one."""
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    (r / "src" / "pkg").mkdir(parents=True)
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

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-q", "-m", "initial"], cwd=r, capture_output=True)
    return r


def run_gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/docs_gate.py", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


def commit_msg(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "COMMIT_MSG"
    f.write_text(body, encoding="utf-8")
    return f


def pr_event(tmp_path: Path, *, title: str = "feat: a thing", body: str = "") -> Path:
    f = tmp_path / "event.json"
    f.write_text(json.dumps({"pull_request": {"title": title, "body": body}}),
                 encoding="utf-8")
    return f


# --- the surface that was already covered, kept as the control ----------------------------


def test_a_tracked_file_still_blocks_it(repo: Path):
    (repo / "docs" / "THING.md").write_text(
        f"<!-- BUDGET: 50 -->\n# Thing\n\nReach it at {PLANTED}.\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    out = run_gate(repo, "--mode", "pre-commit")
    assert out.returncode == 1, f"the file scan stopped blocking:\n{out.stdout}"
    assert "host-identifier" in out.stdout


# --- the two surfaces that did not ---------------------------------------------------------


def test_a_commit_message_blocks_it(repo: Path, tmp_path: Path):
    msg = commit_msg(tmp_path, f"fix: a thing\n\nProved it against {PLANTED} directly.\n")
    out = run_gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert out.returncode == 1, f"the commit message was not scanned:\n{out.stdout}"
    assert "commit-message" in out.stdout


def test_a_pull_request_body_blocks_it(repo: Path, tmp_path: Path):
    event = pr_event(tmp_path, body=f"Ran it against {PLANTED} and it answered.")
    out = run_gate(repo, "--mode", "ci", "--pr-event", str(event))
    assert out.returncode == 1, f"the body was not scanned:\n{out.stdout}"
    assert "public-text" in out.stdout


def test_a_pull_request_title_blocks_it(repo: Path, tmp_path: Path):
    event = pr_event(tmp_path, title=f"fix: reach {PLANTED} on boot")
    out = run_gate(repo, "--mode", "ci", "--pr-event", str(event))
    assert out.returncode == 1, f"the title was not scanned:\n{out.stdout}"
    assert "public-text" in out.stdout


# --- the other direction, on every surface --------------------------------------------------
#
# A scan that blocked every host:port would pass all four tests above. These are what
# distinguishes the fix from that, and they are per-surface for the same reason the tests
# above are: a shared implementation is the fix, not an assumption the tests may make.


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_a_placeholder_passes_in_a_commit_message(repo: Path, tmp_path: Path, placeholder):
    msg = commit_msg(tmp_path, f"docs: a thing\n\nSet the endpoint to {placeholder}.\n")
    out = run_gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert out.returncode == 0, f"a placeholder was blocked:\n{out.stdout}"


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_a_placeholder_passes_in_a_pull_request_body(repo: Path, tmp_path: Path, placeholder):
    event = pr_event(tmp_path, body=f"Set the endpoint to {placeholder} and restart.")
    out = run_gate(repo, "--mode", "ci", "--pr-event", str(event))
    assert out.returncode == 0, f"a placeholder was blocked:\n{out.stdout}"


def test_an_ordinary_message_with_no_host_at_all_passes(repo: Path, tmp_path: Path):
    msg = commit_msg(tmp_path, "docs: a thing\n\nReached the head node on its port.\n")
    out = run_gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert out.returncode == 0, f"a clean message was blocked:\n{out.stdout}"


# --- the drift that allowed it -------------------------------------------------------------


def test_the_commit_message_check_and_the_pull_request_check_share_one_scanner():
    """The two copies is what let them disagree, so the fix is that there is one.

    Asserted structurally rather than behaviourally: the tests above would all still pass
    if someone reintroduced a second copy that happened to be correct today, and it is the
    second copy -- not its current contents -- that is the defect.
    """
    source = GATE.read_text(encoding="utf-8")
    assert source.count("def scan_text(") == 1
    assert source.count("def hostport_leaks(") == 1
    # The commit-message check must call the shared scanner rather than re-implement it.
    start = source.index("def check_commit_message(")
    end = source.index("\ndef ", start + 10)
    body = source[start:end]
    assert "scan_text(" in body, "check_commit_message no longer uses the shared scanner"
    assert "HOST_PATTERNS" not in body, "a second copy of the identifier scan came back"
