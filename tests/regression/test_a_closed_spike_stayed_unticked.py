"""A spike that had been answered sat marked open, and nothing compared the two.

PLAN.md is the contract, but no commit is obliged to touch it: #155 through #160 landed
without changing it at all, and the work in #159 that finished one item never reached the
roadmap. A second item was plainer still -- its own marker line read "Spike answered"
while the marker beside it said open, and it stayed that way for four days.

`check_roadmap_markers` compares the marker against the item's own first line. Asserting
both directions matters more than usual here, because the obvious wrong implementation --
search the file for "Spike answered" -- passes a fires-on-violation test and then blocks
every correctly ticked item as well. Three of the cases below exist only to tell those
two apart.

The gate is run as a subprocess rather than the function imported, so that forgetting to
register the check in CHECKS fails these tests too. A check nobody calls cannot fire.

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

OPEN, DONE = "⬜", "✅"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository the gate can be pointed at.

    The gate resolves ROOT from its own location, so copying the script into a temp tree
    makes that tree the repository under test. The directory is named neutrally on
    purpose: a fixture named after the pattern under test is how an earlier check in this
    repository passed against the bug it was written for.
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


def roadmap(repo: Path, *items: str) -> None:
    repo.joinpath("PLAN.md").write_text(
        "# Roadmap\n\n## Open\n\n" + "\n".join(items) + "\n", encoding="utf-8")


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


def test_an_answered_spike_marked_open_is_blocked(repo: Path):
    """The bug, in the shape PLAN.md line 233 actually had."""
    roadmap(repo, f"- {OPEN} **Spike answered** -- a status subcommand, since a TUI cannot",
                  "  run inside an agent's shell")
    assert fired(gate(repo), "roadmap-marker", "PLAN.md line 5", "Spike answered")


def test_the_same_phrase_on_a_ticked_item_is_silent(repo: Path):
    """The discriminator. A search for the phrase alone blocks this too, and must not.

    This is the shape of PLAN.md line 177, which was correct all along.
    """
    roadmap(repo, f"- {DONE} 2026-09-10 **Spike answered** -- does the client consume",
                  "  skills served over MCP")
    assert not fired(gate(repo), "roadmap-marker")


def test_an_open_item_whose_body_answers_a_sub_question_is_silent(repo: Path):
    """An item may settle one question inside itself and stay genuinely open.

    The streaming entry does exactly this, which is why only the marker line is read.
    """
    roadmap(repo, f"- {OPEN} **Streaming, reopened with a scope.** Filed and cancelled the",
                  "  same day, and the cancellation missed a second consumer",
                  "  - **Answered 2026-09-05:** thinking tokens do count against it")
    assert not fired(gate(repo), "roadmap-marker")


def test_an_unanswered_spike_is_silent(repo: Path):
    """`**Spike**` is not `**Spike answered**`. PLAN.md line 192 is still open."""
    roadmap(repo, f"- {OPEN} **Spike** -- find the cause behind withholding the shell on a",
                  "  verifying pass, rather than writing the workaround down")
    assert not fired(gate(repo), "roadmap-marker")


def test_the_dated_answered_form_is_blocked(repo: Path):
    """The second declaration form, which no live item happens to use today.

    Written because an alternative nothing exercises is an alternative nobody knows works.
    """
    roadmap(repo, f"- {OPEN} **Answered 2026-09-07** -- a detached launch outlives the call",
                  "  that started it")
    assert fired(gate(repo), "roadmap-marker", "PLAN.md line 5", "Answered 2026-09-07")


def test_a_repository_without_a_roadmap_does_not_block(repo: Path):
    """Absence is not a violation. The gate ships to checkouts that have no PLAN.md."""
    lines = gate(repo)
    assert not fired(lines, "roadmap-marker")
    assert any("[roadmap-marker]" in ln and "SKIP" in ln for ln in lines)
