"""`--install-skills`: putting the shipped skills where a caller's tools will find them.

The point of M10 is a host holding only the package. So the assertion that matters is not
"a file was copied" but "the discovery code finds it and parses it", which is what
`survey_agents` answers -- the same call the shipped spec tells a caller to validate with.

`--init`'s rule applies unchanged: an existing file is moved aside, never overwritten and
never left in place. Refusing would leave a half-installed tree with no route forward but
hand-editing; overwriting would destroy an edit someone made deliberately.
"""

from __future__ import annotations

from pathlib import Path

from claude_delegate_local import agents, install_skills
from claude_delegate_local.config import Config

SHIPPED = "write-delegate-agent"


def project_config(root: Path) -> Config:
    return Config(  # type: ignore[arg-type]
        workspace_roots=(str(root),),
        agents_dir=str(root / "home" / "agents"),
    )


def test_an_installed_skill_is_found_and_parses(tmp_path, capsys) -> None:
    """The end-to-end claim M10 makes, minus the wheel."""
    project = tmp_path / "somewhere"
    project.mkdir()

    written = install_skills.install(project, out=None, stamp="20260910-000000")

    assert [p.parent.name for p in written] == [SHIPPED]
    listing = agents.survey_agents(project_config(tmp_path), str(project))
    assert [a.name for a in listing.agents] == [SHIPPED], (
        f"installed but not usable: skipped={[(s.name, s.reason) for s in listing.skipped]} "
        f"other_format={[f.name for f in listing.other_format]}"
    )


def test_nothing_is_found_before_it_runs(tmp_path) -> None:
    """The negative control for the test above.

    A `survey_agents` that reported an agent regardless -- reading the installed package
    rather than the project, say -- would satisfy the assertion above while proving
    nothing about the copy.
    """
    project = tmp_path / "somewhere"
    project.mkdir()

    listing = agents.survey_agents(project_config(tmp_path), str(project))

    assert listing.agents == ()
    assert listing.skipped == ()
    assert listing.other_format == ()


def test_an_existing_skill_is_moved_aside_rather_than_overwritten(tmp_path) -> None:
    """Someone's own edit survives a second run, and is findable afterwards."""
    project = tmp_path / "somewhere"
    target = project / ".claude" / "skills" / SHIPPED / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: write-delegate-agent\n---\n\nMine.\n", encoding="utf-8")

    install_skills.install(project, out=None, stamp="20260910-000000")

    backups = sorted(target.parent.glob("SKILL.md.bak-*"))
    assert len(backups) == 1, (
        f"the previous file was not preserved: {list(target.parent.iterdir())}"
    )
    assert backups[0].read_text(encoding="utf-8").strip().endswith("Mine.")
    assert "Mine." not in target.read_text(encoding="utf-8")


def test_the_installed_copy_matches_what_ships(tmp_path) -> None:
    """A copy that silently diverged from the shipped file would document a format nobody
    is running. Byte equality, not "looks similar"."""
    project = tmp_path / "somewhere"
    project.mkdir()

    written = install_skills.install(project, out=None, stamp="20260910-000000")

    source = install_skills.SKILLS_ROOT / SHIPPED / "SKILL.md"
    assert written[0].read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
