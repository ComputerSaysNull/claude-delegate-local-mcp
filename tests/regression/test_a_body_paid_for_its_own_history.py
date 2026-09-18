"""A skill or agent body carried its own history, and every invocation paid for it.

`CONTRIBUTING.md` says a body states the rule and never the incident behind it: both are
re-read on every invocation, so a sighting, a date or a pull request number in one is
charged on every run, where the same fact in `CHANGELOG.md` is read once by someone asking
why. That rule had nothing enforcing it, and one session put narrative into four separate
bodies before a reader caught it by hand.

An audit would catch it eventually. It runs a few times a month, against an authoring habit
that produces several instances an hour, so the correction has to arrive at commit time.

Two exemptions are load-bearing rather than convenient:

  * **frontmatter** -- `description` is a one-line summary a client displays, and the date
    inside a *quoted* description would otherwise be unfixable without breaking the field;
  * **fenced code blocks** -- `test-writer`'s example asserts on `ADR-0099`, a deliberately
    fictional reference in an illustration. Flagging it would force the example to be broken
    to satisfy the checker, which is the tail wagging the dog.

Tracked documents are deliberately out of scope. `CHANGELOG.md`, `JOURNAL.md` and
`DECISIONS.md` exist to hold exactly what this refuses, so a check that read them would be
telling the project to delete its own record.

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

# Named rather than inlined into the bracketed form below, because the meta-test in
# `test_gate_checks_can_fail.py` scans the test tree for each registry key in quotes and
# `"[body-history]"` does not contain `"body-history"`. A check whose only mention is
# bracketed reads to that test as a check nobody tests.
CHECK = "body-history"

MANIFEST = """\
[docs."docs/PARENT.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/real.py"]
covers_not = "Nothing."

[unowned]
paths = ["tests/**", "scripts/**", "security/**", ".claude/**"]
"""

CLEAN_BODY = """\
---
name: example
description: "Does one thing, and says what it is."
---

# Example

State the rule. A body is re-read on every invocation, so narrative costs every run.
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository the gate can be pointed at.

    The directory is named `r` rather than after anything this test looks for: a fixture
    path that happens to match the pattern under test has silently pruned a tree here
    before, and the assertion passed against the unfixed bug.
    """
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    (r / "src").mkdir()
    (r / ".claude" / "agents").mkdir(parents=True)
    (r / ".claude" / "skills" / "example").mkdir(parents=True)
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
    (r / ".claude" / "agents" / "example.md").write_text(CLEAN_BODY, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    return r


def gate(repo: Path, *args: str) -> list[str]:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", *(args or ("--mode", "pre-commit"))],
        cwd=repo, capture_output=True, text=True,
    )
    return proc.stdout.splitlines()


def fired(lines: list[str], *needles: str) -> bool:
    return any(
        f"[{CHECK}]" in ln
        and ("BLOCK" in ln or "WARN" in ln)
        and all(n in ln for n in needles)
        for ln in lines
    )


def write_agent(repo: Path, body: str) -> None:
    (repo / ".claude" / "agents" / "example.md").write_text(body, encoding="utf-8")


# --- fires on a real violation ----------------------------------------------------------

def test_a_date_in_a_body_is_blocked(repo: Path):
    """The fix. A dated sighting is the commonest shape and the cheapest to spot."""
    write_agent(repo, CLEAN_BODY + "\nMeasured 2026-09-12: six passes came back at low.\n")

    assert fired(gate(repo), "example.md")


def test_a_pull_request_number_in_a_body_is_blocked(repo: Path):
    """The other half. A `#NNN` is a pointer into history that only history can resolve."""
    write_agent(repo, CLEAN_BODY + "\nThe contaminated rate was fixed in #172.\n")

    assert fired(gate(repo), "example.md")


def test_a_skill_body_is_checked_too(repo: Path):
    """Skills carry more of this than agents do, and cost the same per invocation."""
    skill = repo / ".claude" / "skills" / "example" / "SKILL.md"
    skill.write_text(CLEAN_BODY + "\nThis broke nine fixtures on 2026-09-03.\n",
                     encoding="utf-8")

    assert fired(gate(repo), "SKILL.md")


# --- stays silent on the closest thing that is not one -----------------------------------

def test_a_clean_body_is_silent(repo: Path):
    """Control. A check that always fires passes the test above and is worthless."""
    write_agent(repo, CLEAN_BODY)

    assert not fired(gate(repo))


def test_a_date_inside_frontmatter_is_exempt(repo: Path):
    """Control. `description` is displayed by the client and cannot be reflowed freely."""
    write_agent(repo, CLEAN_BODY.replace(
        'description: "Does one thing, and says what it is."',
        'description: "Does one thing. Superseded the 2026-09-12 approach."'))

    assert not fired(gate(repo))


def test_a_reference_inside_a_fenced_block_is_exempt(repo: Path):
    """Control, and the one that would force an example to be broken to satisfy a grep."""
    write_agent(repo, CLEAN_BODY + "\n```python\n"
                "def test_dangling():\n"
                '    assert check(text) == ["ADR-0001 -> ADR-0099"]\n'
                "```\n")

    assert not fired(gate(repo))


def test_a_fence_that_closes_does_not_exempt_what_follows(repo: Path):
    """Control on the control. An exemption that never ends would swallow the whole file."""
    write_agent(repo, CLEAN_BODY + "\n```python\nx = 1\n```\n\nShipped on 2026-09-12.\n")

    assert fired(gate(repo), "example.md")


def test_a_tracked_document_is_not_checked(repo: Path):
    """Control, and the reason the scope is what it is.

    `CHANGELOG.md` and its siblings exist to hold dates and pull request numbers. A check
    that read them would be asking the project to delete its own record.
    """
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## #172 — 2026-09-12 — fix: a thing\n", encoding="utf-8")

    assert not fired(gate(repo))
