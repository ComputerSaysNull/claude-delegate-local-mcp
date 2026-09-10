"""The worked example in the shipped skill did not parse in the format it describes.

`write-delegate-agent` ships inside the package and says of itself: "This file is itself a
valid agent file in the format it describes, so it doubles as the worked example." It was
not. Its example spelled two list fields as bare comma-separated text --
`allowed_tools: read_file, write_file, run_bash` -- where `_coerce_list` requires a flow
sequence and refuses anything else. A caller on a host holding only the package, following
the skill exactly, wrote a file `list_agents` reported under `skipped`.

That is M10's exit condition failing: the skill is the whole of what travels, so an example
it cannot parse is not a wording bug. Nothing caught it because every existing test builds
its frontmatter inline; none read the shipped example back through the parser.

The rule this file is written to (CLAUDE.md): a check that cannot fail is worse than no
check. So `test_a_broken_example_is_caught` feeds the extractor a real violation and
asserts it fires -- without it, an extractor that silently found zero examples would pass
here forever while the skill said anything it liked.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from claude_delegate_local import agents
from claude_delegate_local.agents import AgentError
from claude_delegate_local.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SKILL = REPO_ROOT / "src/claude_delegate_local/skills/write-delegate-agent/SKILL.md"

# A fenced block whose body opens on a frontmatter delimiter is an agent file being shown.
# Anything else in the skill (a shell line, a table) is not, and is not this test's business.
_FENCE = re.compile(r"^```[a-zA-Z]*\n(---\n.*?)^```", re.MULTILINE | re.DOTALL)


def worked_examples(text: str) -> list[str]:
    return [match.group(1) for match in _FENCE.finditer(text)]


def load(tmp_path: pathlib.Path, name: str, body: str):
    """Put `body` where the personal tier looks, and load it as the format really would."""
    home = tmp_path / "home" / "agents"
    home.mkdir(parents=True, exist_ok=True)
    (home / f"{name}.md").write_text(body, encoding="utf-8")
    cfg = Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),),
        agents_dir=str(home),
    )
    return agents.load_agent(cfg, name)


def test_the_skill_actually_contains_a_worked_example() -> None:
    """Guards the extractor itself: zero examples must never read as zero failures."""
    assert worked_examples(SKILL.read_text(encoding="utf-8")), (
        "no fenced agent file found in the skill -- the extractor is broken, or the "
        "skill stopped carrying the worked example it claims to be"
    )


def test_every_worked_example_parses(tmp_path: pathlib.Path) -> None:
    """The bug. Red before the fix: `allowed_tools` was not a flow sequence."""
    for index, example in enumerate(worked_examples(SKILL.read_text(encoding="utf-8"))):
        name = re.search(r"^name:\s*(\S+)", example, re.MULTILINE)
        assert name, f"example {index} has no `name`, so its filename cannot be derived"
        spec = load(tmp_path, name.group(1), example)
        assert spec.name == name.group(1)


def test_a_broken_example_is_caught(tmp_path: pathlib.Path) -> None:
    """The negative control. Without this, the check above could not be trusted to fire."""
    broken = "---\nname: probe\nallowed_tools: read_file, run_bash\n---\nbody\n"
    with pytest.raises(AgentError) as caught:
        load(tmp_path, "probe", broken)
    assert "is not a list" in str(caught.value)


def test_the_bracketed_form_is_what_parses(tmp_path: pathlib.Path) -> None:
    """The other half of the control: the form the fix moves the skill to must work."""
    good = "---\nname: probe\nallowed_tools: [read_file, run_bash]\n---\nbody\n"
    spec = load(tmp_path, "probe", good)
    assert spec.allowed_tools == ("read_file", "run_bash")
