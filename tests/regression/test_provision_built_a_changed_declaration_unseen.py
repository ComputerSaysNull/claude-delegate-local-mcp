"""`provision` rebuilt from a changed dependency declaration without showing it (PLAN M13.9, R5).

`provision` runs `pip install --editable`, and so the project's own build backend, on the
host, outside the sandbox. A delegation can edit the declaration, `--doctor` then reports
the environment stale, and the natural response is to re-provision -- which ran whatever
the edited declaration named, unseen. The fix records a copy of each declaration beside the
environment, and a rebuild from a changed one prints the diff and needs `--yes`. The design
is docs/specs/2026-09-23-host-acted-paths.md.

The build steps are stubbed: what is under test is whether they run.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from claude_delegate_local import provision
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(os.name != "posix", reason="provision builds POSIX trees")


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'p'\ndependencies = ['a']\n", encoding="utf-8")
    home = tmp_path / "home"
    cfg = Config(workspace_roots=(str(tmp_path),), sandbox_home=str(home))  # type: ignore[arg-type]
    builds: list[str] = []

    def fake_run(argv, *, out):
        if "venv" in argv:
            builds.append(argv[-1])
            os.makedirs(os.path.join(argv[-1], "bin"), exist_ok=True)
            Path(argv[-1], "bin", "python").write_text("", encoding="utf-8")

    monkeypatch.setattr(provision, "_run", fake_run)
    monkeypatch.setattr(provision.paths, "resolve_workdir", lambda cfg, given: given)
    monkeypatch.setattr(provision, "denylist_matches", lambda cfg, venv: [])
    return cfg, project, builds


def _provision(cfg, project, *, yes=False):
    out = io.StringIO()
    # `yes` only when set, so the route cases also drive the unfixed `run`, which has no
    # such argument, and fail there on what it does rather than on its signature.
    extra = {"yes": True} if yes else {}
    code = provision.run(cfg, project.as_posix(), out=out, **extra)
    return code, out.getvalue()


def _edit(project: Path):
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'p'\ndependencies = ['a', 'planted']\n", encoding="utf-8")


@posix_only
def test_a_changed_declaration_is_shown_and_not_built(setup):
    cfg, project, builds = setup
    assert _provision(cfg, project)[0] == 0
    _edit(project)
    code, out = _provision(cfg, project)
    assert code != 0, out
    assert len(builds) == 1, "the changed declaration was built unseen"
    assert "+dependencies = ['a', 'planted']" in out, out
    assert "--yes" in out, out


@posix_only
def test_yes_builds_it(setup):
    cfg, project, builds = setup
    _provision(cfg, project)
    _edit(project)
    code, out = _provision(cfg, project, yes=True)
    assert code == 0, out
    assert len(builds) == 2


@posix_only
def test_an_unchanged_declaration_rebuilds_without_asking(setup):
    """The control: re-provisioning what was already built needs nothing new."""
    cfg, project, builds = setup
    _provision(cfg, project)
    code, out = _provision(cfg, project)
    assert code == 0, out
    assert len(builds) == 2


@posix_only
def test_the_first_build_does_not_ask(setup):
    """Nothing to compare against, and the operator named the project themselves."""
    cfg, project, builds = setup
    code, out = _provision(cfg, project)
    assert code == 0, out
    assert builds


@posix_only
def test_a_record_without_a_copy_still_asks(setup):
    """An environment built before copies were kept: the change cannot be shown, so it is
    said rather than built past."""
    cfg, project, builds = setup
    _provision(cfg, project)
    venv = Path(provision.venv_dir(provision.resolve_home(cfg), project.as_posix()))
    record = json.loads((venv / provision.RECORD_NAME).read_text(encoding="utf-8"))
    record.pop("declarations", None)
    (venv / provision.RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    _edit(project)
    code, out = _provision(cfg, project)
    assert code != 0, out
    assert len(builds) == 1
    assert "cannot be shown" in out, out
