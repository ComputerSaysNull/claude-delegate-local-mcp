"""This repository's own agent files: they parse, and the caller/agent split holds.

`docs-audit-local.md` carried two audiences. Fifty-two lines of it told a *caller* how to
size, split and fan out a pass -- opening, in its own words, "Yours to read, not to act
on" -- which every pass then paid to read on every turn while being unable to act on any of
it. That half now lives in `.claude/skills/docs-audit-dispatch/SKILL.md`.

The split is a property of two files at once, so a test that reads only one cannot see it
put back. Both directions are asserted here: absent from the agent, present in the skill.

The rule this file is written to (CLAUDE.md): assert the check fires on a real violation.
`test_the_marker_would_catch_a_revert` builds the pre-split file in a throwaway tree and
asserts the same predicate rejects it -- without that, a typo in the marker string would
make every assertion below vacuously true.
"""

from __future__ import annotations

import pathlib

from claude_delegate_local import agents
from claude_delegate_local.config import Config

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / ".claude/agents/docs-audit-local.md"
DISPATCH = ROOT / ".claude/skills/docs-audit-dispatch/SKILL.md"

# The section's own first words, and the reason it is caller-side rather than agent-side.
CALLER_MARKER = "Yours to read, not to act on"


def holds_caller_guidance(text: str) -> bool:
    return CALLER_MARKER in text


def test_the_agent_body_is_free_of_caller_guidance() -> None:
    assert not holds_caller_guidance(AGENT.read_text(encoding="utf-8")), (
        f"{AGENT.name} carries caller-side guidance again; it belongs in {DISPATCH.parent.name}"
    )


def test_the_dispatch_skill_carries_it() -> None:
    """The other half. Deleting the section outright would pass the test above alone."""
    assert holds_caller_guidance(DISPATCH.read_text(encoding="utf-8"))


def test_the_marker_would_catch_a_revert(tmp_path: pathlib.Path) -> None:
    """The negative control: the predicate must reject the shape that existed before."""
    pre_split = (
        AGENT.read_text(encoding="utf-8")
        + "\n## How a caller should size and split a pass\n\n"
        + CALLER_MARKER
        + ": you audit what you are given.\n"
    )
    probe = tmp_path / "pre-split.md"
    probe.write_text(pre_split, encoding="utf-8")
    assert holds_caller_guidance(probe.read_text(encoding="utf-8"))


def test_both_files_parse_as_agents() -> None:
    """`list_agents` is what the format says to validate with, so validate with it."""
    cfg = Config(workspace_roots=(str(ROOT),))  # type: ignore[arg-type]
    listing = agents.survey_agents(cfg, str(ROOT))
    names = {spec.name for spec in listing.agents}
    assert {"docs-audit-local", "docs-audit-dispatch"} <= names
    assert not listing.skipped, [entry.reason for entry in listing.skipped]
