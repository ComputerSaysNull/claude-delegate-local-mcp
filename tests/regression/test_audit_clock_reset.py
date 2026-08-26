"""Only a documentation audit may reset the audit-due counter.

The bug: the upstream review added in the previous commit landed in `docs/audits/`, and the
staleness check took the alphabetically last `*.md` there as "the last recorded audit". Real
audits are named `YYYY-MM-DD-audit.md`, so anything starting with a letter sorts after all of
them and wins permanently. The counter went from 14 -- the whole history, correct, because no
documentation audit has ever run -- to 0, silently, for a record that is not an audit at all.

Two independent faults, and it needed both:

1. Two kinds of record shared one directory. Upstream reviews now live in `docs/reviews/`.
2. The check dated a *filename* rather than asking git. Filename order is only ever right
   while one naming scheme is in use, and nothing enforced that.

Fixing only the first would leave the trap armed for the next person who puts something in
that directory. These tests hold both halves. See ADR-0025.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"
STALE_THRESHOLD = 60


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
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "src" / "pkg").mkdir(parents=True)
    (r / "docs" / "audits").mkdir(parents=True)
    (r / "docs" / "reviews").mkdir(parents=True)
    (r / "security").mkdir()

    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(
        '[docs."docs/THING.md"]\n'
        'audience = ["contributor"]\n'
        'plane = "product"\n'
        'owns = ["src/pkg/**"]\n'
        'covers_not = "nothing"\n'
        '\n[unowned]\npaths = ["scripts/**", "security/**", "docs/audits/**",'
        ' "docs/reviews/**"]\n',
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


def churn(repo: Path, n: int, tag: str = "c") -> None:
    """Commits that touch neither the document nor its owned code."""
    for i in range(n):
        (repo / f"noise_{tag}_{i}.txt").write_text(str(i), encoding="utf-8")
        commit(repo, f"{tag} {i}")


def stale_findings(repo: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
        cwd=repo, capture_output=True, text=True,
    )
    return [ln.strip() for ln in proc.stdout.splitlines()
            if "audit-due" in ln and "since the last recorded audit" in ln]


def test_the_warning_fires_with_no_audit_at_all(repo: Path) -> None:
    """Baseline. Without this the other tests could pass on a check that never fires."""
    churn(repo, STALE_THRESHOLD + 1)
    assert stale_findings(repo), (
        "a repository past the threshold with no audit on record must warn")


def test_a_real_audit_resets_the_counter(repo: Path) -> None:
    churn(repo, STALE_THRESHOLD + 1)
    assert stale_findings(repo), "precondition: the warning is up"
    (repo / "docs" / "audits" / "2026-01-01-audit.md").write_text("# Audit\n", encoding="utf-8")
    commit(repo, "docs: audit")
    assert not stale_findings(repo), "committing a real audit must clear the warning"


def test_a_review_does_not_reset_the_counter(repo: Path) -> None:
    """The regression. A non-audit record must not silence a real signal."""
    churn(repo, STALE_THRESHOLD + 1)
    assert stale_findings(repo), "precondition: the warning is up"
    (repo / "docs" / "reviews" / "upstream-review-2026-08-26.md").write_text(
        "# Upstream review\n", encoding="utf-8")
    commit(repo, "docs: upstream review")
    assert stale_findings(repo), (
        "an upstream review is not a documentation audit and must not reset the counter")


def test_a_later_filename_committed_earlier_does_not_win(repo: Path) -> None:
    """The second fault, on its own: recency must come from git, not from sort order.

    `zz-audit.md` sorts last but is committed first. If the check dated the filename it
    would treat the stale one as current and stay quiet; asking git, it does not.
    """
    (repo / "docs" / "audits" / "zz-audit.md").write_text("# Audit\n", encoding="utf-8")
    commit(repo, "docs: audit zz")
    churn(repo, STALE_THRESHOLD + 1, tag="after")
    assert stale_findings(repo), (
        "the newest *commit* in docs/audits/ is what counts; the last filename is not")

    (repo / "docs" / "audits" / "2026-01-01-audit.md").write_text("# Audit\n", encoding="utf-8")
    commit(repo, "docs: audit dated")
    assert not stale_findings(repo), (
        "a fresh audit clears the warning even though its name sorts before an older file")


def test_the_repository_keeps_reviews_out_of_the_audit_directory() -> None:
    """Guards the split itself, against this repository rather than a fixture."""
    audits = list((ROOT / "docs" / "audits").glob("*.md"))
    for p in audits:
        assert p.name[:4].isdigit(), (
            f"{p.name} is in docs/audits/ but is not a dated audit. Only documentation "
            f"audits reset the staleness counter; other records belong in docs/reviews/.")
