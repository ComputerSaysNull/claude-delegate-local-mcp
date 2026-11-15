"""`_build_into` recorded the dependency hash it took after the build, not before (PLAN M13.9).

The record's `dependency_hash` and the declaration copy kept for the next rebuild's diff were
both read *after* the install finished -- minutes after phase 1 had already read the same
declaration and downloaded its wheels. A delegation can edit the declaration while a build
runs, so the record could claim a declaration the interpreter was not built from, and
`is_current` would then call that interpreter current: `interpreter_for` hands it out and a
test run passes against what the project no longer declares.

The fix takes the hash and the declaration copy before the build starts, and writes those
into the record, so the record says what was read rather than what happened to be there
afterwards.

The build steps are stubbed -- `_run` and `sandbox.run` both -- so no real `bwrap` and no
network run here; what is under test is which declaration reaches the record.

Named after the bug, per the project's convention.
"""

import io
import json
from pathlib import Path

import pytest

from claude_delegate_local import provision, sandbox
from claude_delegate_local.config import Config

BEFORE = (
    '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    '[project]\nname = "demo"\nversion = "0.0.0"\n'
    'dependencies = ["httpx>=0.28"]\n'
)
AFTER = BEFORE.replace('"httpx>=0.28"', '"httpx>=0.28", "planted>=1"')


def _cfg(home: Path, project: Path) -> Config:
    return Config(  # type: ignore[arg-type]
        workspace_roots=(str(project.parent),), sandbox_home=str(home)
    )


@pytest.fixture
def built(tmp_path: Path, monkeypatch):
    """A package project mid-build, with the declaration rewritten during the install."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(BEFORE, encoding="utf-8")
    home = tmp_path / "home"
    (home / "venvs").mkdir(parents=True)
    venv = provision.venv_dir(home.as_posix(), project.as_posix())
    Path(venv, "bin").mkdir(parents=True)
    Path(venv, "bin", "python").write_text("", encoding="utf-8")

    def fake_run(argv, *, out, cwd=None):
        return None

    def fake_sandbox_run(cfg, req):
        if "--editable" in req.command:
            # The install is the last step before the record is written, minutes after phase 1
            # read the declaration. This is the edit a delegation would make meanwhile.
            (project / "pyproject.toml").write_text(AFTER, encoding="utf-8")
            return sandbox.SandboxResult(stdout="", stderr="", exit_code=0, timed_out=False)
        return sandbox.SandboxResult(
            stdout='DELEGATE_BUILD_REQUIRES=[]\n', stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr(provision, "_run", fake_run)
    monkeypatch.setattr(provision.sandbox, "run", fake_sandbox_run)

    before_hash = provision.dependency_hash(project.as_posix())
    before_copy = provision.declarations(project.as_posix())
    provision._build_into(
        _cfg(home, project), project.as_posix(), venv, home.as_posix(), out=io.StringIO()
    )
    record = json.loads((Path(venv) / provision.RECORD_NAME).read_text(encoding="utf-8"))
    after_hash = provision.dependency_hash(project.as_posix())
    return record, before_hash, before_copy, after_hash


def test_the_edit_really_moved_the_hash(built):
    """The control: without this, the assertion below could pass on a hash that never moved."""
    _, before_hash, _, after_hash = built
    assert before_hash != after_hash


def test_the_record_names_the_hash_the_build_started_from(built):
    record, before_hash, _, after_hash = built
    assert record["dependency_hash"] == before_hash, (
        "the record names the declaration as it stood after the build"
    )
    assert record["dependency_hash"] != after_hash


def test_the_kept_copy_is_the_text_the_build_read(built):
    """The copy exists so the next rebuild can show the diff. A copy of the post-build text
    would diff the edit against itself and show nothing."""
    record, _, before_copy, _ = built
    assert record["declarations"] == before_copy
    assert record["declarations"]["pyproject.toml"] == BEFORE
    assert record["declarations"]["pyproject.toml"] != AFTER


def test_the_environment_is_stale_because_the_declaration_moved(built):
    """The harm the bug did: `is_current` trusted an interpreter built from the old text."""
    record, _, _, _ = built
    assert provision.is_current(record) is False
