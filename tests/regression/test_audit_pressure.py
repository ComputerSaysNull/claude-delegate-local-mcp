"""The audit-due check must actually fire.

Replaces a calendar with evidence: a document is suspect when the code it owns has moved
and it has not. In a fresh repository every document was written alongside its code, so
the pressure reads zero everywhere -- which is correct, and is also indistinguishable
from a check that cannot fire at all.

This project has already shipped three checks that reported success while verifying
nothing, so a green result on real data is not accepted as proof. These tests build a
repository where the condition genuinely holds.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=False).stdout.strip()


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", message],
        cwd=repo, capture_output=True, check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repository the gate can run against."""
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "src" / "pkg").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "security").mkdir()

    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(
        '[docs."docs/THING.md"]\n'
        'audience = ["contributor"]\n'
        'plane = "product"\n'
        'owns = ["src/pkg/**"]\n'
        'covers_not = "nothing"\n'
        "\n[unowned]\npaths = [\"scripts/**\", \"security/**\"]\n",
        encoding="utf-8",
    )
    (r / "security" / "allowed_emails.txt").write_text("t@example.com\n", encoding="utf-8")
    (r / "security" / "secret_globs.txt").write_text(".env\n", encoding="utf-8")
    (r / "security" / "content_safe_emails.txt").write_text("t@example.com\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    (r / "docs" / "THING.md").write_text("<!-- BUDGET: 50 -->\n# Thing\n", encoding="utf-8")
    (r / "src" / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit(r, "initial")
    return r


def audit_findings(repo: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
        cwd=repo, capture_output=True, text=True,
    )
    return [ln.strip() for ln in proc.stdout.splitlines() if "audit-due" in ln]


def test_no_pressure_when_code_and_doc_move_together(repo: Path):
    for i in range(20):
        (repo / "src" / "pkg" / "a.py").write_text(f"x = {i}\n", encoding="utf-8")
        (repo / "docs" / "THING.md").write_text(
            f"<!-- BUDGET: 50 -->\n# Thing\nrev {i}\n", encoding="utf-8")
        commit(repo, f"both {i}")
    assert not [f for f in audit_findings(repo) if "THING.md" in f], (
        "a document kept in step with its code is not suspect"
    )


def test_pressure_builds_when_only_the_code_moves(repo: Path):
    """The case the check exists for, and the one a fresh repository cannot exhibit."""
    for i in range(20):
        (repo / "src" / "pkg" / "a.py").write_text(f"x = {i}\n", encoding="utf-8")
        commit(repo, f"code only {i}")

    findings = [f for f in audit_findings(repo) if "THING.md" in f]
    assert findings, "20 commits of owned-code churn must raise the document"
    assert "20 commits" in findings[0], findings[0]


def test_pressure_resets_when_the_document_is_updated(repo: Path):
    for i in range(20):
        (repo / "src" / "pkg" / "a.py").write_text(f"x = {i}\n", encoding="utf-8")
        commit(repo, f"code only {i}")
    assert [f for f in audit_findings(repo) if "THING.md" in f]

    (repo / "docs" / "THING.md").write_text(
        "<!-- BUDGET: 50 -->\n# Thing\nreviewed\n", encoding="utf-8")
    commit(repo, "docs: review THING")
    assert not [f for f in audit_findings(repo) if "THING.md" in f], (
        "updating the document must clear the pressure, or the warning becomes permanent "
        "noise and gets ignored"
    )


def test_it_warns_rather_than_blocks(repo: Path):
    """Pressure is a prompt for judgement, not a verdict.

    Blocking here would force a documentation edit to land unrelated work, which is how
    a useful signal turns into a rubber stamp."""
    for i in range(20):
        (repo / "src" / "pkg" / "a.py").write_text(f"x = {i}\n", encoding="utf-8")
        commit(repo, f"code only {i}")
    findings = [f for f in audit_findings(repo) if "THING.md" in f]
    assert findings and findings[0].startswith("WARN"), findings
