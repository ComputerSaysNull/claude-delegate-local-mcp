"""This repository's agent and skill files: both readers accept them, and the split holds.

An agent file has two readers and they disagree about what is valid. `agents.py` parses
frontmatter line by line and strips balanced quotes; every editor, and Claude Code itself,
parses it as YAML. The bespoke one is the **more permissive**, which is the trap: a
description reading `... not a role to dispatch: it has no body ...` has an unquoted `: `
inside a scalar, so YAML raises `mapping values are not allowed here` while `list_agents`
reports the file as perfectly fine. That shipped in a commit and was found by opening the
file in an editor, not by any check.

So the invariant is **both readers, every file** -- and the YAML half is the one nothing
else covers.

The rule this file is written to (CLAUDE.md): a check that cannot fail is worse than no
check. Each check here has a sibling that feeds it a real violation and asserts it fires.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from claude_delegate_local import agents
from claude_delegate_local.config import Config

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / ".claude/agents/docs-audit-local.md"
DISPATCH = ROOT / ".claude/skills/docs-audit-dispatch/SKILL.md"

# The value that broke YAML while the server's own parser accepted it.
POISON = 'description: Guidance for dispatching it. Not a role to dispatch: it has no body\n'


def frontmatter_files() -> list[pathlib.Path]:
    """Every file either reader will try to parse as frontmatter."""
    found = sorted(ROOT.glob(".claude/agents/*.md"))
    found += sorted(ROOT.glob(".claude/skills/*/SKILL.md"))
    found += sorted(ROOT.glob("src/claude_delegate_local/skills/*/SKILL.md"))
    return found


def frontmatter_of(text: str) -> str:
    """The opening block only. A `---` further down the body is just text."""
    if not text.startswith("---"):
        raise AssertionError("no frontmatter block")
    return text.split("---", 2)[1]


def parses_as_yaml(text: str) -> bool:
    try:
        loaded = yaml.safe_load(frontmatter_of(text))
    except yaml.YAMLError:
        return False
    return isinstance(loaded, dict)


def test_there_are_files_to_check() -> None:
    """Guards the collector: an empty glob must never read as an empty set of failures."""
    assert len(frontmatter_files()) >= 5


@pytest.mark.parametrize("path", frontmatter_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_frontmatter_parses_as_real_yaml(path: pathlib.Path) -> None:
    """The check nothing else performs. Editors and Claude Code read these as YAML."""
    text = path.read_text(encoding="utf-8")
    assert parses_as_yaml(text), (
        f"{path.relative_to(ROOT).as_posix()} frontmatter is not valid YAML. The server's "
        f"own parser is more permissive and will not tell you: quote any value containing "
        f"a colon followed by a space."
    )


def test_the_yaml_check_rejects_the_shape_that_shipped() -> None:
    """The negative control. Without it, a broken extractor would pass every file above."""
    broken = f"---\nname: probe\n{POISON}---\n\nbody\n"
    assert not parses_as_yaml(broken)


def test_a_quoted_colon_is_the_fix() -> None:
    """The other half: the repair must actually repair, not merely differ."""
    fixed = '---\nname: probe\ndescription: "Not a role to dispatch: it has no body"\n---\n\nbody\n'
    assert parses_as_yaml(fixed)


def test_the_server_parser_accepts_them_too(tmp_path: pathlib.Path) -> None:
    """Both readers, not just the strict one -- a YAML-clean file the server refuses is
    equally broken, and quoting is the shape that has to satisfy both."""
    home = tmp_path / "agents"
    home.mkdir(parents=True)
    (home / "docs-audit-local.md").write_text(AGENT.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),), agents_dir=str(home)
    )
    spec = agents.load_agent(cfg, "docs-audit-local")
    assert spec.allowed_tools == ("read_file", "search_files", "read_git")


# --- the caller/agent split -----------------------------------------------------------------

# A class name appears only where the classes are *defined*, which is the dispatch skill.
DEFINED_ONLY_IN_SKILL = ("CROSS-PLANE LEAK", "ESCAPE ABUSE", "TOO VERBOSE")


def test_the_check_definitions_live_in_the_skill() -> None:
    body = DISPATCH.read_text(encoding="utf-8")
    for name in DEFINED_ONLY_IN_SKILL:
        assert name in body, f"{name} is defined nowhere"


def test_the_agent_body_does_not_redefine_them() -> None:
    """The split. The agent is told its class by the task, so it carries no menu."""
    body = AGENT.read_text(encoding="utf-8")
    leaked = [name for name in DEFINED_ONLY_IN_SKILL if name in body]
    assert not leaked, f"{leaked} moved back into the agent body; they belong to the caller"


def test_the_split_check_would_catch_a_revert() -> None:
    """The negative control for the two above."""
    reverted = AGENT.read_text(encoding="utf-8") + "\n**CROSS-PLANE LEAK** — a fact in both.\n"
    assert [n for n in DEFINED_ONLY_IN_SKILL if n in reverted]
