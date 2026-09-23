"""A failed re-provision deleted the environment that worked (PLAN M13.9.a, .d).

`build` removed the existing virtualenv before building the new one, so a rebuild that
failed -- network down, a bad pin -- left no interpreter at all where one had been working.
And an `OSError` from that removal, or from writing the build record, surfaced as a
traceback, because `run` catches only `ProvisionError` and a timeout.

The fix moves the old environment aside, builds in place, and puts the old one back if the
build fails. Not "build aside, then rename", which is what PLAN first said: a virtualenv
writes its own absolute path into its scripts -- 26 of the 31 files in this machine's
`bin/` carry it -- so a tree built at one path and renamed to another is broken.

The build steps are stubbed: what is under test is what happens to the directories, and a
real `pip install` would make that a network test.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from claude_delegate_local import provision
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(os.name != "posix", reason="provision builds POSIX trees")


def _cfg(home: Path) -> Config:
    return Config(workspace_roots=(str(home.parent),), sandbox_home=str(home))  # type: ignore[arg-type]


def _stub(monkeypatch, *, fail_on: str | None):
    """`_run` that makes the venv directory and fails on the step naming `fail_on`."""
    def fake_run(argv, *, out):
        if "venv" in argv:
            os.makedirs(os.path.join(argv[-1], "bin"), exist_ok=True)
            Path(argv[-1], "bin", "python").write_text("new\n", encoding="utf-8")
        if fail_on is not None and fail_on in argv:
            raise provision.ProvisionError(f"{argv[0]} exited 1")
    monkeypatch.setattr(provision, "_run", fake_run)


@pytest.fixture
def built(tmp_path: Path):
    """A project with an environment already provisioned for it."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'p'\n", encoding="utf-8")
    home = tmp_path / "home"
    venv = Path(provision.venv_dir(str(home), project.as_posix()))
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("old\n", encoding="utf-8")
    return _cfg(home), project.as_posix(), venv


@posix_only
def test_a_failed_rebuild_leaves_the_old_environment_in_place(built, monkeypatch):
    cfg, project, venv = built
    _stub(monkeypatch, fail_on="--editable")
    with pytest.raises(provision.ProvisionError):
        provision.build(cfg, project, out=io.StringIO())
    assert (venv / "bin" / "python").read_text(encoding="utf-8") == "old\n"
    assert not Path(f"{venv}.previous").exists(), "the aside copy outlived the restore"


@posix_only
def test_a_successful_rebuild_replaces_it(built, monkeypatch):
    """The control: the new environment lands at the same path and nothing is left aside."""
    cfg, project, venv = built
    _stub(monkeypatch, fail_on=None)
    assert provision.build(cfg, project, out=io.StringIO()) == str(venv)
    assert (venv / "bin" / "python").read_text(encoding="utf-8") == "new\n"
    assert (venv / provision.RECORD_NAME).exists()
    assert not Path(f"{venv}.previous").exists()


@posix_only
def test_an_unwritable_record_is_a_clean_error(built, monkeypatch):
    """`run` reports a `ProvisionError` in one line; any other exception is a traceback."""
    cfg, project, venv = built
    _stub(monkeypatch, fail_on=None)
    real = Path.write_text

    def refuse_record(self, *a, **kw):
        if self.name == provision.RECORD_NAME:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", refuse_record)
    with pytest.raises(provision.ProvisionError, match="Permission denied"):
        provision.build(cfg, project, out=io.StringIO())
    assert (venv / "bin" / "python").read_text(encoding="utf-8") == "old\n"


@posix_only
def test_run_reports_the_failure_in_words(built, monkeypatch):
    cfg, project, _ = built
    _stub(monkeypatch, fail_on="--editable")
    out = io.StringIO()
    monkeypatch.setattr(provision.paths, "resolve_workdir", lambda cfg, given: given)
    assert provision.run(cfg, project, out=out) == 1
    assert "previous environment" in out.getvalue(), out.getvalue()
