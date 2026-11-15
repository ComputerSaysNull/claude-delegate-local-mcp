"""Three `--doctor` checks answered OK for a state a delegation then failed in.

Each of these is the same shape as the four checks this repository has already found that
could not fail: the check asked about something adjacent to the thing that breaks, so the
report said the environment was fine and the operator went looking somewhere else.

(a) A record whose `interpreter` is empty passed the existence test, because `Path("")` is
    the current directory and it always exists. `--doctor` then said the interpreter was
    present while every command that wanted it found nothing.
(b) The ledger check wrote a probe file in the ledger's *folder* and never opened the
    ledger file itself. A read-only ledger in a writable folder reported OK, and every
    runtime append -- which is swallowed to `logging` by design -- failed in silence.
(c) `discover` listed every directory under the provisioned root, so a build's staging
    folder and a `<venv>.previous` left by an interrupted rebuild were reported as
    environments with no record: two FAILs naming a state nothing was wrong with.

Named after the bug, per the project's convention.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import doctor, provision
from claude_delegate_local.config import Config
from claude_delegate_local.doctor import FAIL

PROJECT = "/mnt/c/Users/dev/proj"


def cfg(**over) -> Config:
    kw = {"workspace_roots": (PROJECT,)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def provisioned(home: Path, project: Path, **over) -> Path:
    """An environment shaped the way `provision` leaves one, without building it.

    Paths go in POSIX-shaped for the same reason `tests/test_doctor.py` does: `venv_name`
    takes a POSIX basename deliberately.
    """
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


def declared(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "d"\n', encoding="utf-8")
    return project


# --- (a) a record naming no interpreter -------------------------------------------------


def test_a_record_naming_no_interpreter_fails(tmp_path):
    """`Path("")` is the current directory, so an empty string passed the existence test.

    The control is `test_a_current_environment_passes` in `tests/test_doctor.py`: the same
    tree, with the interpreter recorded, is OK.
    """
    home = tmp_path / "home"
    provisioned(home, declared(tmp_path), interpreter="")

    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))

    assert [c.verdict for c in checks] == [FAIL]
    assert doctor.worst(checks) == FAIL
    assert "interpreter" in checks[0].detail


# --- (b) a ledger file that refuses the append ------------------------------------------


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root opens a 0444 file regardless of its mode, so the case cannot arise",
)
def test_a_read_only_ledger_file_fails(tmp_path):
    """The folder is writable and the file is not, which is the case a probe in the folder misses.

    `ledger.append` swallows the `OSError` to `logging`, so the doctor is the only place
    this is reported and the running total stops without anyone being told.
    """
    target = tmp_path / "ledger.jsonl"
    target.write_text("", encoding="utf-8")
    target.chmod(0o444)

    check = doctor.check_ledger(cfg(ledger_path=str(target)))

    assert check.verdict == FAIL
    assert str(target) in check.detail


# --- (c) a build's own folders listed as environments ------------------------------------


def test_discovery_skips_a_staging_folder_and_a_previous_one(tmp_path):
    """Both are left by `provision.build`, and neither is an environment.

    The names come from the build's own helpers, so a rename there moves this with it: the
    staging folder from `staging_dir`, the previous one from the `f"{venv}.previous"` that
    `build` moves the live environment to while it rebuilds.
    """
    home = tmp_path / "home"
    venv = provisioned(home, declared(tmp_path))
    staging = Path(provision.staging_dir(home.as_posix(), (tmp_path / "proj").as_posix()))
    staging.mkdir(parents=True)
    previous = Path(str(venv) + ".previous")
    previous.mkdir()

    found = provision.discover(home.as_posix())

    assert [path for path, _record in found] == [str(venv)]
    assert found[0][1] is not None

    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))
    assert [c.verdict for c in checks] == [doctor.OK]
