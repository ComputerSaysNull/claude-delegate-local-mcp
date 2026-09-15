"""`check_roadmap_budget`: three lines per bullet, and only where the body is not frozen.

PLAN.md grew by accretion -- each correction appended to the bullet it corrected -- until
half its items ran past ten lines and the roadmap read as an archive. The cap pushes history
to the documents that own it and leaves the item saying what is to be done.

Two boundaries decide whether this check is honest, and both have a test here:

- **A bullet ends at the first blank line.** The alternative, running to the next bullet,
  swallows the prose paragraphs that sit between items and charges them to whichever bullet
  happened to precede one. That was a real miscount while this was being written.
- **A frozen body is exempt.** ✅ and ❌ bodies may not be reworded, and 192 of the 311 lines
  a blanket cap would have had to remove sat inside them. A cap that forced those edits
  would be a rule contradicting a rule, so it applies to live subtrees only.

Sub-bullets carry their own three lines. That is what makes the cap livable rather than a
deletion order: a long item becomes a short parent with short children, and only genuine
history leaves.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

HEADER = "<!-- BUDGET: 400 -->\n# Plan\n\n## Open\n\n### M1 — a milestone\n\n"
LINE = "  continuation line that is long enough to be real prose and not a stub\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "wt"
    for d in ("scripts", "docs", "security", "src"):
        (r / d).mkdir(parents=True, exist_ok=True)
    (r / "scripts" / "docs_gate.py").write_bytes((ROOT / "scripts" / "docs_gate.py").read_bytes())
    (r / "scripts" / "docs_ownership.toml").write_bytes(
        (ROOT / "scripts" / "docs_ownership.toml").read_bytes())
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n"),
        ("secret_globs.txt", ".env\n"),
        ("content_safe_emails.txt", "t@example.com\n"),
        ("forbidden_strings.txt", "# local\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    return r


def plan(repo: Path, body: str) -> None:
    (repo / "PLAN.md").write_text(HEADER + body, encoding="utf-8")


def gate(repo: Path) -> list[str]:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
        cwd=repo, capture_output=True, text=True,
    )
    return proc.stdout.splitlines()


def fired(lines: list[str], *needles: str) -> bool:
    return any(
        "[roadmap-budget]" in ln
        and ("BLOCK" in ln or "WARN" in ln)
        and all(n in ln for n in needles)
        for ln in lines
    )


def test_fires_on_a_four_line_item(repo: Path):
    plan(repo, "1. ⬜ **Too long**\n" + LINE * 3)
    assert fired(gate(repo), "4 lines")


def test_silent_on_a_three_line_item(repo: Path):
    """The other direction. A check that flagged every bullet would pass the test above."""
    plan(repo, "1. ⬜ **Just right**\n" + LINE * 2)
    assert not fired(gate(repo))


def test_fires_on_a_long_child(repo: Path):
    plan(repo, "1. ⬜ **Fine**\n    - a. ⬜ **Child too long**\n" + "     x\n" * 3)
    assert fired(gate(repo), "4 lines")


def test_a_child_gets_its_own_three_lines(repo: Path):
    """The escape valve: splitting is the remedy, so a split must actually be one."""
    plan(repo, "1. ⬜ **Fine**\n" + LINE * 2 + "    - a. ⬜ **Child**\n" + "     x\n" * 2)
    assert not fired(gate(repo))


def test_a_frozen_body_is_exempt(repo: Path):
    """✅ bodies may not be reworded, so the cap must not demand it."""
    plan(repo, "1. ✅ 2026-09-14 **Done and long**\n" + LINE * 6)
    assert not fired(gate(repo))


def test_a_cancelled_body_is_exempt(repo: Path):
    plan(repo, "1. ❌ 2026-09-14 **Cancelled and long**\n" + LINE * 6)
    assert not fired(gate(repo))


def test_a_bullet_ends_at_the_first_blank_line(repo: Path):
    """The prose paragraph between items belongs to neither, and is charged to neither."""
    plan(repo, "1. ⬜ **Short**\n" + LINE + "\nA paragraph of prose that sits\nbetween items.\n")
    assert not fired(gate(repo))


def test_a_note_counts_too(repo: Path):
    """An unmarked child is still a bullet. Otherwise the cap is opt-out: move the prose
    into a dash and it never bites again."""
    plan(repo, "1. ⬜ **Fine**\n    - a. ✅ a note that runs on\n" + "     x\n" * 3)
    assert fired(gate(repo), "4 lines")
