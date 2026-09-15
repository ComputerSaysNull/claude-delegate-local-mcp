"""`check_roadmap_ids` asserted in both directions, before it existed.

PLAN.md items were referred to by quoting their titles, which is why a session plan citing
one had to paste a sentence and hope it still matched. Items are numbered instead -- `1.` at
the top level, `a.` beneath, with the status marker after the number -- so `M11.1b` names one
thing for as long as it exists. The id *is* the list marker, so an item still written with a
dash is exactly an item with no id.

Three things this file exists to stop, all of them ways a check like this quietly does
nothing:

- **An id that is merely absent.** The fires-direction tests are the real red: run against a
  gate with no such check they fail, because nothing reports `[roadmap-id]` at all.
- **A check that fires on everything.** Every case here has its silent twin. The silent twins
  *pass before the check exists* -- nothing fires, so nothing over-fires -- and only start
  carrying weight once it does. One direction alone is not a test.
- **A fixture that matches by accident.** The temp PLAN.md is written per test rather than
  copied from the real one, so a passing assertion cannot be an artefact of the repository's
  own current contents.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

HEADER = "<!-- BUDGET: 400 -->\n# Plan\n\n## Open\n\n### M1 — a milestone\n\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository shaped only enough for the roadmap checks to run in it."""
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
        "[roadmap-id]" in ln
        and ("BLOCK" in ln or "WARN" in ln)
        and all(n in ln for n in needles)
        for ln in lines
    )


# ------------------------------------------------------------------ an item needs an id

def test_fires_when_a_top_level_item_is_still_a_dash(repo: Path):
    plan(repo, "- ⬜ **An item that nobody can refer to**\n")
    assert fired(gate(repo), "no id")


def test_silent_when_every_item_is_numbered(repo: Path):
    plan(repo, "1. ⬜ **An item that can be cited as M1.1**\n")
    assert not fired(gate(repo))


def test_fires_on_a_marked_child_that_is_still_a_dash(repo: Path):
    plan(repo, "1. ⬜ **Parent**\n   - ⬜ **A child that is work but cannot be cited**\n")
    assert fired(gate(repo), "no id")


def test_fires_on_an_unmarked_child(repo: Path):
    """Every sub-bullet owes an id, note or not, or the rule is opt-out: drop the marker
    and the bullet stops being citable."""
    plan(repo, "1. ⬜ **Parent**\n   - **A trap recorded beside it, which is not a task**\n")
    assert fired(gate(repo), "no id")


def test_silent_on_a_top_level_note(repo: Path):
    """A struck original, or a correction filed beside a frozen body, is not an item."""
    plan(repo, "1. ✅ 2026-09-14 **Done**\n- **Correction filed beside it, not edited in**\n")
    assert not fired(gate(repo))


# ------------------------------------------------------------------------ ids are unique

def test_fires_when_two_items_share_an_id(repo: Path):
    plan(repo, "1. ⬜ **First**\n1. ⬜ **Second, wearing the first one's id**\n")
    assert fired(gate(repo), "repeats")


def test_silent_when_ids_differ(repo: Path):
    plan(repo, "1. ⬜ **First**\n2. ⬜ **Second**\n")
    assert not fired(gate(repo))


def test_the_same_id_may_repeat_under_a_different_milestone(repo: Path):
    """Ids are scoped to their section -- M1.1 and M2.1 are different items."""
    plan(repo, "1. ⬜ **First**\n\n### M2 — another milestone\n\n1. ⬜ **Also first**\n")
    assert not fired(gate(repo))


def test_a_child_letter_is_read_with_its_parent(repo: Path):
    """`a.` under 1 and `a.` under 2 are 1a and 2a, and do not collide."""
    plan(repo, "1. ⬜ **First**\n    - a. ⬜ **Child**\n2. ⬜ **Second**\n    - a. ⬜ **Child**\n")
    assert not fired(gate(repo))


# -------------------------------------------------------- a parent waits for its children

def test_fires_when_a_done_parent_has_an_open_child(repo: Path):
    plan(repo, "1. ✅ 2026-09-14 **Parent**\n    - a. ⬜ **Still outstanding**\n")
    assert fired(gate(repo), "done while")


def test_silent_when_every_marked_child_is_done(repo: Path):
    plan(repo, "1. ✅ 2026-09-14 **Parent**\n    - a. ✅ **Finished**\n")
    assert not fired(gate(repo))


def test_an_open_parent_with_an_open_child_is_fine(repo: Path):
    """The rule constrains ticking a parent, not having unfinished children."""
    plan(repo, "1. ⬜ **Parent**\n    - a. ⬜ **Outstanding**\n")
    assert not fired(gate(repo))


# ------------------------------------------------------------------ a child nests visibly

def test_fires_on_a_three_space_child(repo: Path):
    """`1. ` puts content at column 3 and `19. ` at column 4, so three spaces nests under
    one and renders beside the other. Nothing checked this until it happened twice."""
    plan(repo, "1. ⬜ **Parent**\n   - a. ⬜ **Child one space short**\n")
    assert fired(gate(repo), "4")


def test_silent_on_a_four_space_child(repo: Path):
    plan(repo, "19. ⬜ **Parent**\n    - a. ⬜ **Child that clears a two-digit number**\n")
    assert not fired(gate(repo))
