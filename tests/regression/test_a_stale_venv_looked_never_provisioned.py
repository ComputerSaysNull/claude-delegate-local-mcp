"""A stale provisioned venv read as "never provisioned" to a delegation and its caller.

`provisioned_for` withholds a stale environment rather than offering it, so `sandbox_env`
returns `{}` and `$DELEGATE_PYTHON` is simply absent inside `run_bash`. That is the same
shape as a project that was never provisioned, and neither the model nor the MCP caller is
told at run time that an environment exists but is stale -- so a delegation quietly runs no
test at all, and says nothing about why.

`stale_for` finds the matching record `provisioned_for` refused to return, and
`stale_env_note` turns that into a sentence a delegation reads beside the result body. The
caller also gets `provisioning_stale` in the delegation result.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import provision, sandbox, tools
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "PROVISIONING UNPROVEN BY THIS RUN -- this is not a pass. It builds a POSIX "
        "virtualenv the sandbox binds, and refuses on Windows by design."
    ),
)


def cfg(**over) -> Config:
    kw = {"workspace_roots": ("/mnt/c/Users/dev/proj",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def project_tree(tmp_path: Path) -> Path:
    """One minimal project, with a declaration that can be moved on from."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    return root


def recorded(home: Path, project: Path, **over) -> Path:
    """One provisioned environment, shaped as `provision` leaves it, without a real build."""
    venv = Path(provision.venv_dir(home.as_posix(), project.as_posix()))
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    record = {
        "project": project.as_posix(),
        "interpreter": (venv / "bin" / "python").as_posix(),
        "dependency_hash": provision.dependency_hash(project.as_posix()),
        "hash_sources": list(provision.HASH_SOURCES[:1]),
    }
    record.update(over)
    (venv / provision.RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    return venv


def make_stale(home: Path, project: Path) -> None:
    """Record an environment, then move the declaration on so it reads stale."""
    recorded(home, project)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "moved-on"\n', encoding="utf-8"
    )


# --- stale_for --------------------------------------------------------------------------


@posix_only
def test_stale_for_returns_the_record_when_it_is_stale(tmp_path):
    """The withholding this is for: the record exists but `provisioned_for` refuses it."""
    home = tmp_path / "home"
    project = project_tree(tmp_path)
    make_stale(home, project)

    found = provision.stale_for(cfg(sandbox_home=str(home)), project.as_posix())

    assert found is not None
    assert found["project"] == project.as_posix()


@posix_only
def test_stale_for_returns_none_when_the_record_is_current(tmp_path):
    """A current environment is offered, not withheld, so it is not "stale"."""
    home = tmp_path / "home"
    project = project_tree(tmp_path)
    recorded(home, project)

    assert provision.stale_for(cfg(sandbox_home=str(home)), project.as_posix()) is None


@posix_only
def test_stale_for_returns_none_when_nothing_was_provisioned(tmp_path):
    """Absent is a different answer from stale, and only stale is reported."""
    home = tmp_path / "home"
    project = project_tree(tmp_path)

    assert provision.stale_for(cfg(sandbox_home=str(home)), project.as_posix()) is None


# --- stale_env_note ---------------------------------------------------------------------


@posix_only
def test_the_note_names_the_variable_when_stale(tmp_path):
    """The model must be told which name is unset and why, not just that something is wrong."""
    home = tmp_path / "home"
    project = project_tree(tmp_path)
    make_stale(home, project)

    note = tools.stale_env_note(cfg(sandbox_home=str(home)), project.as_posix())

    assert note
    assert "$DELEGATE_PYTHON" in note
    assert "stale" in note.lower()


@posix_only
def test_the_note_is_none_when_current(tmp_path):
    home = tmp_path / "home"
    project = project_tree(tmp_path)
    recorded(home, project)

    assert tools.stale_env_note(cfg(sandbox_home=str(home)), project.as_posix()) is None


@posix_only
def test_the_note_is_none_when_nothing_was_provisioned(tmp_path):
    home = tmp_path / "home"
    project = project_tree(tmp_path)

    assert tools.stale_env_note(cfg(sandbox_home=str(home)), project.as_posix()) is None


# --- the note reaches the run_bash result ----------------------------------------------


@posix_only
def test_run_bash_appends_the_note_to_the_result_body(tmp_path, monkeypatch):
    """The note is said beside the output, where the model reads the result of its command.

    `_run_bash` needs no bubblewrap when `sandbox.run` is stubbed, so this drives the real
    text-assembly path rather than a helper that could drift from it.
    """
    home = tmp_path / "home"
    project = project_tree(tmp_path)
    make_stale(home, project)
    monkeypatch.setattr(
        sandbox, "run",
        lambda c, r: sandbox.SandboxResult(
            stdout="hello", stderr="", exit_code=0, timed_out=False
        ),
    )

    result = tools._run_bash(
        cfg(sandbox_home=str(home), workspace_roots=(str(project),)),
        {"command": "ls"},
        tools.BashPolicy(workdir=str(project)),
    )

    assert "$DELEGATE_PYTHON" in result.text
    assert "stale" in result.text.lower()
