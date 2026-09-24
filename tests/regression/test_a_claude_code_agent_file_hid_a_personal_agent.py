"""A Claude Code agent file in the project tier hid a valid personal agent of the same name.

The lookup took the first file that existed, so a project's `.claude/delegate-agents/helper.md`
written for Claude Code -- a `tools:` key, which is that format and not a broken copy of
this one -- was refused as invalid, and the personal `helper.md` behind it was never tried.
The listing did the same: the foreign file claimed the name, so the usable agent vanished.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_delegate_local import agents
from claude_delegate_local.agents import AgentError
from claude_delegate_local.config import Config

CLAUDE_CODE_FILE = "---\nname: helper\ndescription: theirs\ntools: Read, Grep\n---\n\nTheirs.\n"
OURS = "---\nname: helper\n---\n\nYou help.\n"
BROKEN_OURS = "---\nname: helper\nnot_a_field: 1\n---\n\nYou help.\n"


def _setup(tmp_path: Path, project_file: str) -> tuple[Config, Path]:
    c = Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "home" / "agents"),
    )
    work = tmp_path / "proj"
    for path, text in (
        (work / ".claude" / "delegate-agents" / "helper.md", project_file),
        (Path(c.agents_dir) / "helper.md", OURS),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return c, work


def test_the_lookup_passes_over_a_claude_code_file_to_the_personal_agent(tmp_path) -> None:
    c, work = _setup(tmp_path, CLAUDE_CODE_FILE)
    spec = agents.load_agent(c, "helper", str(work))
    assert spec.body.strip() == "You help."


def test_the_listing_shows_the_personal_agent_behind_a_claude_code_file(tmp_path) -> None:
    c, work = _setup(tmp_path, CLAUDE_CODE_FILE)
    listing = agents.survey_agents(c, str(work))
    assert [a.name for a in listing.agents] == ["helper"]
    assert [f.name for f in listing.other_format] == ["helper"]


def test_a_broken_file_in_this_format_still_refuses_rather_than_falling_through(tmp_path) -> None:
    """The other half: a project's own broken agent is its problem to fix, and quietly
    running the personal one instead would hide that it is broken."""
    c, work = _setup(tmp_path, BROKEN_OURS)
    with pytest.raises(AgentError, match="not_a_field"):
        agents.load_agent(c, "helper", str(work))
