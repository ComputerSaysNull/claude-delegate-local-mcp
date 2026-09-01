"""A budget header that records why it was raised was invisible to the budget check.

Found by the 2026-09-01 audit. `check_budgets` searched for `BUDGET: n` followed by the
comment terminator with only whitespace between them. CHANGELOG #23 introduced the
convention of recording each raise's reason *inside* the same comment, which puts prose
between the number and the terminator, so the search failed and `if not m: continue`
skipped the document in silence. Three documents had been growing unenforced --
docs/ARCHITECTURE.md 221 lines past its own declared cap -- and the gate reported PASS.

The `SKIP` branch could not reveal it either: it fires only when *no* document declares a
budget at all, so three losing theirs produced no signal.

Both instruments had the anchor, so `BUDGET-PER-ENTRY` is tested here too; it was one edit
away from the same silence.

Every case is asserted in both directions, per the rule this project learned the hard way:
a check that never fires passes a silent-on-clean test, and one that always fires passes a
fires-on-violation test. Only the pair tells them apart.

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

MANIFEST = """\
[docs."docs/PARENT.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/real.py"]
covers_not = "Nothing."

[unowned]
paths = ["tests/**", "scripts/**", "security/**", "src/other.py"]
"""

# The shape the convention actually produces: the number, then the reason, then the
# terminator several lines later. Written as a template so the cap can vary per test.
REASONED = """\
<!-- BUDGET: {cap}      Raised from {prev} on 2026-08-30: a mechanism this document owns
     and could not previously describe, because it did not exist. What it replaced was one
     wrong sentence elsewhere. -->
"""


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
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n"),
        ("secret_globs.txt", ".env\nid_rsa\n"),
        ("content_safe_emails.txt", "t@example.com\n"),
        ("forbidden_strings.txt", "# local\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    (r / "docs" / "PARENT.md").write_text("<!-- BUDGET: 99 -->\n# Parent\n", encoding="utf-8")
    (r / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    return r


def gate(repo: Path) -> list[str]:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
        cwd=repo, capture_output=True, text=True,
    )
    return proc.stdout.splitlines()


def fired(lines: list[str], check: str, *needles: str) -> bool:
    return any(
        f"[{check}]" in ln
        and ("BLOCK" in ln or "WARN" in ln)
        and all(n in ln for n in needles)
        for ln in lines
    )


def test_a_reasoned_budget_still_blocks_a_document_over_it(repo: Path):
    """The bug. Before the fix this passed silently and the gate reported PASS."""
    (repo / "docs" / "big.md").write_text(
        REASONED.format(cap=5, prev=4) + "line\n" * 40, encoding="utf-8")
    assert fired(gate(repo), "budget", "docs/big.md", "against a budget of 5")


def test_a_reasoned_budget_stays_silent_when_the_document_fits(repo: Path):
    """The other direction. A check that blocked every reasoned header would pass above."""
    (repo / "docs" / "small.md").write_text(
        REASONED.format(cap=50, prev=40) + "line\n" * 4, encoding="utf-8")
    assert not fired(gate(repo), "budget", "docs/small.md")


def test_the_single_line_form_still_works(repo: Path):
    """Relaxing the anchor must not cost the shape most documents still use."""
    (repo / "docs" / "plain.md").write_text(
        "<!-- BUDGET: 5 -->\n" + "line\n" * 40, encoding="utf-8")
    assert fired(gate(repo), "budget", "docs/plain.md", "against a budget of 5")


def test_a_reasoned_per_entry_budget_still_fires(repo: Path):
    """`BUDGET-PER-ENTRY` carried the same anchor and would have failed the same way."""
    (repo / "docs" / "log.md").write_text(
        "<!-- BUDGET-PER-ENTRY: 3      Raised on 2026-08-30 because entries grew\n"
        "     a worked example each. -->\n"
        "## short\nx\n## long\n" + "y\n" * 30,
        encoding="utf-8")
    assert fired(gate(repo), "budget", "docs/log.md", "'long'")


def test_a_reasoned_per_entry_budget_stays_silent_on_short_sections(repo: Path):
    (repo / "docs" / "log2.md").write_text(
        "<!-- BUDGET-PER-ENTRY: 30      Raised on 2026-08-30 for the same reason\n"
        "     as its sibling above. -->\n"
        "## a\nx\n## b\ny\n",
        encoding="utf-8")
    assert not fired(gate(repo), "budget", "docs/log2.md")


def test_a_reasoned_total_budget_is_not_read_as_a_per_entry_one(repo: Path):
    """`BUDGET:` must not match `BUDGET-PER-ENTRY:` now that the terminator is gone.

    Without the colon holding them apart, a relaxed pattern could read the per-entry
    header as a total cap and block a long append-only document for the wrong reason --
    trading one silent instrument for a loud and incorrect one.
    """
    (repo / "docs" / "entries.md").write_text(
        "<!-- BUDGET-PER-ENTRY: 40 -->\n## a\n" + "y\n" * 30 + "## b\ny\n",
        encoding="utf-8")
    lines = gate(repo)
    assert not fired(lines, "budget", "docs/entries.md", "against a budget of")
