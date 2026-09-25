"""Every action CI runs is pinned to a commit, not to a tag its owner can move.

A tag such as `@v4` names whatever commit it points at on the day CI runs. Whoever controls
that action's repository -- or anyone who breaks into it -- can move it, and CI then runs
their code with this repository's secrets in reach: the gate job hands
`FORBIDDEN_STRINGS` to a job that also runs `checkout` and `setup-python`. A 40-character
commit hash cannot be moved.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)")
SHA = re.compile(r"[0-9a-f]{40}")


def _unpinned(text: str) -> list[str]:
    return [
        m.group(0).strip()
        for m in map(USES.match, text.splitlines())
        if m and not SHA.fullmatch(m.group("ref"))
    ]


def test_there_are_workflows_to_check():
    """Guards the collector: no workflow found must never read as nothing unpinned."""
    assert sorted(WORKFLOWS.glob("*.yml"))


def test_every_action_is_pinned_to_a_commit():
    unpinned = [
        f"{path.name}: {line}"
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for line in _unpinned(path.read_text(encoding="utf-8"))
    ]
    assert not unpinned, "pin these to a commit hash, with the version in a comment:\n" + "\n".join(
        unpinned
    )


def test_the_check_sees_a_tag_and_accepts_a_hash():
    """Negative control: the reader must flag a tag, or the test above is blind."""
    text = (
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@" + "a" * 40 + "  # v5.6.0\n"
    )
    assert _unpinned(text) == ["- uses: actions/checkout@v4"]
