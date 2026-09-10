"""What the wheel actually carries, asked of the build backend rather than believed.

`docs/AGENTS.md` cannot answer a caller who installed only the package: it lives outside
`src/claude_delegate_local`, the one tree hatchling packages. Measured on 2026-09-10 by
building the wheel -- 21 modules and nothing else. So the agent-file format ships as a
file *inside* the package, and this asserts it stays there.

The build backend is asked directly. `WheelBuilder.recurse_included_files()` is the same
enumeration `pip wheel` drives, minus the network that build isolation needs to fetch
hatchling, and it answers in hundredths of a second rather than tens of seconds. That is
what lets this run in the default suite instead of behind the `integration` marker.
"""

from __future__ import annotations

import pathlib

import pytest
from hatchling.builders.wheel import WheelBuilder

from claude_delegate_local import agents
from claude_delegate_local.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SHIPPED_SKILL = "claude_delegate_local/skills/write-delegate-agent/SKILL.md"

SHIPPED_SKILL_SOURCE = REPO_ROOT / "src" / SHIPPED_SKILL


@pytest.fixture(scope="module")
def shipped_paths() -> frozenset[str]:
    builder = WheelBuilder(str(REPO_ROOT))
    return frozenset(
        f.distribution_path.replace("\\", "/") for f in builder.recurse_included_files()
    )


def test_the_agent_format_skill_is_in_the_wheel(shipped_paths: frozenset[str]) -> None:
    assert SHIPPED_SKILL in shipped_paths, (
        f"{SHIPPED_SKILL} is not in the wheel, so a host holding only the package cannot "
        f"read the agent-file format. Shipped: {sorted(shipped_paths)}"
    )


def test_a_repository_root_document_is_not_in_the_wheel(shipped_paths: frozenset[str]) -> None:
    """The negative control for the test above.

    Without this, an enumeration that wrongly returned every file in the repository would
    satisfy the assertion above while proving nothing. `README.md` and `docs/` sit outside
    the packaged tree, so they must be absent.
    """
    assert "README.md" not in shipped_paths
    assert not any(p.startswith("docs/") for p in shipped_paths)


# --- the shipped spec against the parser it describes --------------------------------------


def _project_with(tmp_path: pathlib.Path, name: str, text: str) -> tuple[Config, str]:
    """Drop one SKILL.md into a throwaway project and return a config that can see it."""
    skill = tmp_path / "proj" / ".claude" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(text, encoding="utf-8")
    cfg = Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),),
        agents_dir=str(tmp_path / "home" / "agents"),
    )
    return cfg, str(tmp_path / "proj")


def test_the_shipped_spec_parses_as_the_format_it_describes(tmp_path) -> None:
    """The spec eats its own dog food.

    If the shipped file documents a format the parser refuses -- a key that is not in
    `KNOWN_FIELDS`, an `effort` level that does not exist -- this fires, because the file
    is written in the format it specifies.
    """
    cfg, workdir = _project_with(
        tmp_path, "write-delegate-agent", SHIPPED_SKILL_SOURCE.read_text(encoding="utf-8")
    )
    listing = agents.survey_agents(cfg, workdir)

    assert [a.name for a in listing.agents] == ["write-delegate-agent"], (
        f"shipped spec did not parse: skipped={[(s.name, s.reason) for s in listing.skipped]} "
        f"other_format={[f.name for f in listing.other_format]}"
    )
    assert listing.agents[0].description, "list_agents would report this agent with no description"


def test_the_bucket_assertion_can_tell_the_buckets_apart(tmp_path) -> None:
    """The negative control for the test above.

    `survey_agents` returning everything it found under `agents` would satisfy the
    assertion above while proving nothing. A file carrying Claude Code's `tools:` key must
    land in `other_format` instead -- which is why `code-reviewer` and three siblings sit
    there in this repository today.
    """
    cfg, workdir = _project_with(
        tmp_path, "borrowed-role", "---\nname: borrowed-role\ntools: Read\n---\n\nYou help.\n"
    )
    listing = agents.survey_agents(cfg, workdir)

    assert [a.name for a in listing.agents] == []
    assert [f.name for f in listing.other_format] == ["borrowed-role"]
