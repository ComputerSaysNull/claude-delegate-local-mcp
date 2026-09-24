"""This server's agent files lived where Claude Code loads its own subagents.

Both read `.claude/agents/`, so a file written for the local model was also offered to a
cloud session as a subagent with every tool -- spending exactly what delegating exists to
save. They now live in `.claude/delegate-agents/`; the old directory is still read, last,
for one release, and `list_agents` says where each agent found there should move.
"""

from __future__ import annotations

from pathlib import Path

from claude_delegate_local import agents
from claude_delegate_local.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "home" / "delegate-agents"),
    )


def _agent(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.stem}\n---\n\n{marker}\n", encoding="utf-8")


def test_an_agent_in_the_new_project_directory_is_found(tmp_path) -> None:
    c, work = _cfg(tmp_path), tmp_path / "proj"
    _agent(work / ".claude" / "delegate-agents" / "helper.md", "NEW")
    assert agents.load_agent(c, "helper", str(work)).body.strip() == "NEW"


def test_the_old_directory_is_read_last_and_named_with_where_to_move(tmp_path) -> None:
    c, work = _cfg(tmp_path), tmp_path / "proj"
    _agent(work / ".claude" / "agents" / "helper.md", "OLD")
    assert agents.load_agent(c, "helper", str(work)).body.strip() == "OLD"
    listing = agents.survey_agents(c, str(work))
    assert [(n, Path(to).parts[-2:]) for n, _src, to in listing.old_location] == [
        ("helper", (".claude", "delegate-agents")),
    ]

    # The personal tier now wins over the old directory ...
    _agent(Path(c.agents_dir) / "helper.md", "PERSONAL")
    assert agents.load_agent(c, "helper", str(work)).body.strip() == "PERSONAL"
    # ... and the new project directory over both.
    _agent(work / ".claude" / "delegate-agents" / "helper.md", "NEW")
    assert agents.load_agent(c, "helper", str(work)).body.strip() == "NEW"
    assert agents.survey_agents(c, str(work)).old_location == ()


def test_the_personal_default_is_not_claude_codes_directory(tmp_path) -> None:
    default = Config(workspace_roots=(str(tmp_path),)).agents_dir  # type: ignore[arg-type]
    assert Path(default).parts[-2:] == (".claude", "delegate-agents")
