"""The gate reported a clean CI run having read no commits at all.

CI runs `docs_gate.py --mode ci --diff <range>`, and `pr_range()` treats a range it is
given as authoritative. On a push to main the range CI builds is `origin/main...HEAD`,
which is empty -- the checkout IS main -- so every range-walking check (identity,
commit-message, split-dodge, prose-regrowth) inspected zero commits and reported a clean
pass. "Check nothing and still report success" is the shape CLAUDE.md names as worse than
no check at all, because it is trusted.

The second half is the waiver parser. `waivers()` read trailers from a hard-coded
`origin/main...HEAD` rather than from the range the checks were given, so the two surfaces
disagreed: a `Docs-Gate-Skip` trailer inside the range under review was invisible, and one
from outside it would have been read.

Each case below is asserted in the direction that fails against the bug: the empty range
must block, and a waiver written inside the given range must take effect. The controls --
a one-commit range, and a waiver outside the range -- are here because a check that
blocked every run, or read every trailer it could find, would pass the first direction
alone.

Named after the bug, per the project's convention.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

# docs_gate.py is copied into the fixture wholesale and its own source names this address
# in a comment, so the fixture has to allowlist it. Split for the same reason
# test_gate_reads_the_real_commit_message.py splits it.
GATE_EMAIL = "noreply@" + "github.com"

MANIFEST = '''[docs."docs/PARENT.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/real.py"]
covers_not = "Nothing."

[unowned]
paths = ["tests/**", "scripts/**", "security/**"]
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository the gate can be pointed at.

    The gate resolves ROOT from its own location, so copying the script into a temp tree
    makes that tree the repository under test. Nothing here touches the real one.
    """
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    (r / "src").mkdir()
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(MANIFEST, encoding="utf-8")
    (r / "docs" / "PARENT.md").write_text("<!-- BUDGET: 99 -->\n# Parent\n", encoding="utf-8")
    (r / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n" + GATE_EMAIL + "\n"),
        ("secret_globs.txt", ".env\nid_rsa\n"),
        ("content_safe_emails.txt", "t@example.com\n" + GATE_EMAIL + "\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    # Must exist for the scan to run and must NOT be tracked: the never-track check blocks
    # on it by design. The `.gitignore` is what keeps `git add -A` from staging it.
    (r / "security" / "forbidden_strings.txt").write_text("", encoding="utf-8")
    (r / ".gitignore").write_text("security/forbidden_strings.txt\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    commit(r, "chore: the base")
    return r


def commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", message],
        cwd=repo, capture_output=True, check=True,
    )


def run_gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/docs_gate.py", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


def findings(stdout: str, check: str) -> list[str]:
    """The report lines one check produced, whatever their level.

    Substring matching on the whole output is not enough: an unrelated SKIP elsewhere
    satisfies `check in stdout` on its own.
    """
    return [ln for ln in stdout.splitlines() if f"[{check}]" in ln]


# ------------------------------------------------------------------- the empty range

def test_an_empty_ci_range_blocks(repo: Path):
    """A range with no commits checked nothing, so it cannot report a pass.

    `HEAD...HEAD` is the shape of the push-to-main range: the merge base of a revision
    with itself is that revision, so the diff and the log are both empty.
    """
    out = run_gate(repo, "--mode", "ci", "--diff", "HEAD...HEAD")
    assert out.returncode == 1, (
        f"an empty range passed, so no commit was checked:\n{out.stdout}{out.stderr}")
    blocks = [ln for ln in findings(out.stdout, "empty-range") if "BLOCK" in ln]
    assert blocks, f"no empty-range finding:\n{out.stdout}"
    assert any("HEAD...HEAD" in ln for ln in blocks), (
        f"the finding does not name the range it read:\n{out.stdout}")


def test_a_one_commit_range_is_not_reported_as_empty(repo: Path):
    """The other direction: a range that holds a commit must be judged, not refused.

    A check that blocked every CI run would pass the test above.
    """
    (repo / "src" / "real.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "docs" / "PARENT.md").write_text(
        "<!-- BUDGET: 99 -->\n# Parent\n\nNow describes the change.\n", encoding="utf-8")
    commit(repo, "fix: change the source and its document")
    out = run_gate(repo, "--mode", "ci", "--diff", "HEAD~1...HEAD")
    assert not findings(out.stdout, "empty-range"), (
        f"a one-commit range was called empty:\n{out.stdout}{out.stderr}")
    assert out.returncode == 0, f"{out.stdout}{out.stderr}"


# ------------------------------------------------------------------------- waivers

def test_a_waiver_inside_the_given_range_is_read(repo: Path):
    """The trailer is written in the range under review, so it must be found there.

    The block it waives is real: `src/real.py` changed and its owning document did not,
    which `owning-doc` blocks. Without that, the assertion holds vacuously.
    """
    (repo / "src" / "real.py").write_text("x = 2\n", encoding="utf-8")
    commit(repo, "fix: change the source\n\nDocs-Gate-Skip: owning-doc -- deliberate\n")
    out = run_gate(repo, "--mode", "ci", "--diff", "HEAD~1...HEAD")
    assert "WAIVED" in out.stdout, (
        f"a waiver inside the range was not read:\n{out.stdout}{out.stderr}")
    assert out.returncode == 0, f"{out.stdout}{out.stderr}"


def test_a_waiver_outside_the_given_range_is_not_read(repo: Path):
    """A trailer from a commit the range does not contain waives nothing.

    The waiver sits in the base commit; the change under review is the commit after it, and
    that one carries no trailer of its own.
    """
    (repo / "src" / "real.py").write_text("x = 2\n", encoding="utf-8")
    commit(repo, "fix: change the source\n\nDocs-Gate-Skip: owning-doc -- deliberate\n")
    (repo / "src" / "real.py").write_text("x = 3\n", encoding="utf-8")
    commit(repo, "fix: change the source again")
    out = run_gate(repo, "--mode", "ci", "--diff", "HEAD~1...HEAD")
    assert "WAIVED" not in out.stdout, (
        f"a waiver from outside the range was applied:\n{out.stdout}{out.stderr}")
    assert out.returncode == 1, (
        f"the owning-doc violation was not blocked:\n{out.stdout}{out.stderr}")
    assert findings(out.stdout, "owning-doc"), out.stdout
