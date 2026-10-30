"""A never-provisioned project with no venv read as "nothing wrong".

`missing_env_note` only fired when a covered virtualenv sat inside the workdir, so a script
project with only a `requirements.txt` -- or a package project with a `pyproject.toml` -- that
was never provisioned got no note at all: the model saw an ImportError, fell back to the
standard library, and never asked for a build. The dependency declaration is as much a sign
that a build is wanted as a hidden venv is, so the note now also fires when the nearest
declaring folder is found -- the workdir, then its parents, stopping at a workdir root and
never above one.

Named after the bug, per the project's convention.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import provision, tools
from claude_delegate_local.config import Config


def cfg(**over) -> Config:
    kw = {"workspace_roots": ("/mnt/c/Users/dev/proj",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def nothing_provisioned(monkeypatch) -> None:
    """No current and no stale environment, so only the missing case can fire."""
    monkeypatch.setattr(provision, "provisioned_for", lambda cfg, wd: None)
    monkeypatch.setattr(provision, "stale_for", lambda cfg, wd: None)


def _provisioned(monkeypatch) -> None:
    monkeypatch.setattr(
        provision, "provisioned_for", lambda cfg, wd: {"interpreter": "/venv/bin/python"}
    )
    monkeypatch.setattr(provision, "stale_for", lambda cfg, wd: None)


def _stale(monkeypatch) -> None:
    monkeypatch.setattr(provision, "provisioned_for", lambda cfg, wd: None)
    monkeypatch.setattr(
        provision, "stale_for", lambda cfg, wd: {"interpreter": "/venv/bin/python"}
    )


# --- the note fires on a declaration ------------------------------------------------


def test_the_note_fires_for_a_workdir_with_only_requirements_txt(tmp_path, monkeypatch):
    """A script project with no venv at all must still be told a build is possible."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home")),
        root.as_posix(), (),
    )

    assert note
    assert root.as_posix() in note
    assert "$DELEGATE_PYTHON" in note
    assert "provision" in note


def test_the_note_names_the_nearest_declaring_parent(tmp_path, monkeypatch):
    """A workdir nested in a package project is provisioned for the project folder, not
    the workdir, so the note names the folder the caller must pass to `provision`."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    work = proj / "work"
    work.mkdir()
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home")),
        work.as_posix(), (),
    )

    assert note
    assert proj.as_posix() in note
    assert work.as_posix() not in note


def test_the_note_names_both_a_hidden_venv_and_the_declaring_folder(tmp_path, monkeypatch):
    """When both are present, both are named: the venv relative to the workdir, the
    declaring folder absolutely."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home")),
        proj.as_posix(), (str(proj / ".venv"),),
    )

    assert note
    assert ".venv" in note
    assert proj.as_posix() in note


# --- negative controls ---------------------------------------------------------------


def test_a_declaration_above_the_workspace_root_is_not_found(tmp_path, monkeypatch):
    """The walk stops at a workdir root; a declaration above it belongs to a different
    project and must not be blamed for this workdir's missing build."""
    root = tmp_path / "root"
    root.mkdir()
    work = root / "proj"
    work.mkdir()
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(workspace_roots=(str(root),), sandbox_home=str(tmp_path / "home")),
        work.as_posix(), (),
    )

    assert note is None


def test_the_note_is_none_with_no_declaration_and_no_venv(tmp_path, monkeypatch):
    """A bare workdir with nothing hidden and nothing declared really is nothing missing."""
    work = tmp_path / "proj" / "work"
    work.mkdir(parents=True)
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home")),
        work.as_posix(), (),
    )

    assert note is None


def test_the_note_is_none_when_provisioned(tmp_path, monkeypatch):
    """A current environment sets $DELEGATE_PYTHON, so nothing is missing."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    _provisioned(monkeypatch)

    assert tools.missing_env_note(
        cfg(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home")),
        root.as_posix(), (),
    ) is None


def test_the_note_is_none_when_stale(tmp_path, monkeypatch):
    """Stale is the other case the note covers; the stale note fires instead, not both."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    _stale(monkeypatch)

    assert tools.missing_env_note(
        cfg(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home")),
        root.as_posix(), (),
    ) is None
