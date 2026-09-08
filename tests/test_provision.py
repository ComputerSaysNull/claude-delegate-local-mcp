"""`provision`: the naming, the record, the staleness digest, and what pip is not told.

Almost nothing here builds a real environment. A virtualenv plus a dependency resolution is
minutes and a network, and none of the properties worth pinning down need either: what
matters is that two checkouts cannot collide on a name, that the record says what it was
built from, that the digest moves when the declaration does, and that an operator's own pip
configuration is cut out of the build. The one case that does need a real build is
`integration`-marked and lives at the bottom.

Every check that can fail is asserted failing. `dependency_hash` returning None for a
missing file, `install_target` dropping `[dev]` for a project that does not declare it, and
`main` refusing both too few arguments and too many -- a usage check that accepted anything
would be the fourth check in this repository that could not fail.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import provision, sandbox
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "PROVISIONING UNPROVEN BY THIS RUN -- this is not a pass. It builds a POSIX "
        "virtualenv the sandbox binds, and refuses on Windows by design."
    ),
)

PROJECT = "/mnt/c/Users/dev/proj"
HOME = "/home/dev/.cache/sandbox-home"


def cfg(**over) -> Config:
    kw = {"workspace_roots": (PROJECT,)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def project_tree(tmp_path: Path, *, extras: str = '\ndev = ["pytest>=8"]\n') -> Path:
    """A minimal project: a name, a version and optionally a dev extra."""
    root = tmp_path / "proj"
    root.mkdir()
    optional = f"[project.optional-dependencies]{extras}" if extras else ""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n' + optional, encoding="utf-8"
    )
    return root


# --- naming ------------------------------------------------------------------------------


def test_two_checkouts_with_the_same_basename_get_different_directories():
    """The collision this digest exists to stop: a worktree, or a second clone.

    Same basename, different dependencies. Without the digest one project silently runs the
    other's interpreter, and the exit code it returns is trusted (ADR-0007).
    """
    first = provision.venv_name("/mnt/c/Users/dev/a/proj")
    second = provision.venv_name("/mnt/c/Users/dev/b/proj")
    assert first != second
    assert first.startswith("proj-") and second.startswith("proj-")


def test_the_name_is_stable_for_one_path():
    assert provision.venv_name(PROJECT) == provision.venv_name(PROJECT)


def test_a_trailing_slash_does_not_change_the_name():
    """`resolve_workdir` normalises, but the digest must not depend on that having happened."""
    assert provision.venv_name("/a/proj").split("-")[0] == \
        provision.venv_name("/a/proj/").split("-")[0]


def test_the_environment_lives_under_the_provisioned_root_and_nowhere_else():
    venv = provision.venv_dir(HOME, PROJECT)
    assert venv.startswith(sandbox.provisioned_root(HOME) + "/")


def test_the_interpreter_is_an_absolute_path_inside_it():
    """Absolute, because `SANDBOX_PATH` is deliberately not widened to reach it.

    POSIX-shaped even when the test runs on Windows: this path becomes a bind, and
    `os.path.join` here would hand a contributor backslashes bwrap cannot use.
    """
    venv = provision.venv_dir(HOME, PROJECT)
    assert provision.interpreter_path(venv) == f"{venv}/bin/python"
    assert "\\" not in provision.interpreter_path(venv)


# --- the staleness digest ----------------------------------------------------------------


def test_the_digest_moves_when_the_declaration_does(tmp_path):
    """The whole point: a changed declaration must not read as current.

    Stale dependencies do not error. They produce a passing run against the wrong versions
    and return 0, which is the one exit code ADR-0007 says to believe.
    """
    root = project_tree(tmp_path)
    before = provision.dependency_hash(str(root))
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.1"\n', encoding="utf-8"
    )
    assert provision.dependency_hash(str(root)) != before


def test_the_digest_is_stable_when_nothing_changes(tmp_path):
    root = project_tree(tmp_path)
    assert provision.dependency_hash(str(root)) == provision.dependency_hash(str(root))


def test_a_project_with_no_declaration_has_no_digest(tmp_path):
    """None rather than a digest of nothing, so `--doctor` can say which case it is."""
    empty = tmp_path / "bare"
    empty.mkdir()
    assert provision.dependency_hash(str(empty)) is None


# --- what gets installed -----------------------------------------------------------------


def test_the_dev_extra_is_requested_when_the_project_declares_it(tmp_path):
    root = project_tree(tmp_path)
    assert provision.install_target(str(root)).endswith("[dev]")


def test_the_dev_extra_is_not_assumed_when_it_is_absent(tmp_path):
    """pip fails outright on an undeclared extra, so guessing costs the whole build."""
    root = project_tree(tmp_path, extras="")
    assert provision.install_target(str(root)) == str(root)


def test_an_unparseable_declaration_falls_back_rather_than_raising(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project\nbroken", encoding="utf-8")
    assert provision.install_target(str(root)) == str(root)


# --- the build environment ---------------------------------------------------------------


def test_the_operators_pip_configuration_is_cut_out(monkeypatch):
    """Nothing scans the finished tree, so this is where the guarantee has to be made.

    A `pip.conf` may carry an index URL with credentials in it, and the secret scan skips
    this tree by design (ADR-0062) -- so build time is the last point at which an operator's
    index configuration can be kept out of it.
    """
    monkeypatch.setenv("PIP_CONFIG_FILE", "/home/dev/.config/pip/pip.conf")
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://example.invalid/extra")

    env = provision.build_env()

    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert "PIP_INDEX_URL" not in env
    assert "PIP_EXTRA_INDEX_URL" not in env


def test_the_rest_of_the_environment_is_left_alone(monkeypatch):
    """The control: a build still needs its CA bundle and its HOME to work at all.

    Without this, `build_env` could pass by returning an empty dict, which would break
    every build while satisfying the assertions above.
    """
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    assert provision.build_env()["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"


# --- the record --------------------------------------------------------------------------


def test_a_missing_record_reads_as_none_rather_than_raising(tmp_path):
    assert provision.read_record(str(tmp_path)) is None


def test_an_unparseable_record_reads_as_none(tmp_path):
    """A half-written record is the case where a test run passes against nothing, so it has
    to be reportable rather than an exception out of a diagnostic."""
    (tmp_path / provision.RECORD_NAME).write_text("{not json", encoding="utf-8")
    assert provision.read_record(str(tmp_path)) is None


def test_a_record_that_is_not_an_object_reads_as_none(tmp_path):
    (tmp_path / provision.RECORD_NAME).write_text("[1, 2]", encoding="utf-8")
    assert provision.read_record(str(tmp_path)) is None


def test_discovery_finds_an_environment_and_its_record(tmp_path):
    """`--doctor` needs no setting to find these, which is the point of the record.

    The expected value is the POSIX string `venv_dir` returned, not a `Path` round-trip:
    `str(Path(...))` rewrites separators on Windows and would compare the wrong shape.
    """
    venv = provision.venv_dir(str(tmp_path), PROJECT)
    Path(venv).mkdir(parents=True)
    (Path(venv) / provision.RECORD_NAME).write_text(
        json.dumps({"project": PROJECT}), encoding="utf-8"
    )

    assert provision.discover(str(tmp_path)) == [(venv, {"project": PROJECT})]


def test_discovery_reports_an_environment_with_no_record_rather_than_hiding_it(tmp_path):
    venv = provision.venv_dir(str(tmp_path), PROJECT)
    Path(venv).mkdir(parents=True)
    assert provision.discover(str(tmp_path)) == [(venv, None)]


def test_discovery_of_an_absent_root_is_empty_rather_than_an_error(tmp_path):
    assert provision.discover(str(tmp_path / "nothing-here")) == []


# --- the denylist report -----------------------------------------------------------------


@posix_only
def test_a_denylist_match_inside_a_provisioned_tree_is_reported(tmp_path):
    """Reported, never covered. Covering is what breaks the environment (ADR-0041)."""
    site = tmp_path / "venv" / "lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "credentials.py").write_text("x", encoding="utf-8")
    (site / "ordinary.py").write_text("x", encoding="utf-8")

    found = provision.denylist_matches(cfg(), str(tmp_path / "venv"))

    assert [Path(p).name for p in found] == ["credentials.py"]


@posix_only
def test_a_clean_tree_reports_nothing(tmp_path):
    """The control, so the reporter cannot pass by matching everything or nothing."""
    site = tmp_path / "venv" / "lib"
    site.mkdir(parents=True)
    (site / "ordinary.py").write_text("x", encoding="utf-8")
    assert provision.denylist_matches(cfg(), str(tmp_path / "venv")) == []


# --- the command's own argument handling -------------------------------------------------


def drive(args: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = provision.main(argv=args, out=out)
    return code, out.getvalue()


@posix_only
def test_no_project_is_a_usage_error(capsys):
    code, _ = drive([])
    assert code == 2
    assert "usage:" in capsys.readouterr().err


@posix_only
def test_two_projects_is_a_usage_error(capsys):
    """It takes one project. Silently provisioning the first would be worse than refusing."""
    code, _ = drive(["/a", "/b"])
    assert code == 2
    assert "usage:" in capsys.readouterr().err


@posix_only
def test_a_project_outside_every_workdir_root_is_refused(tmp_path, monkeypatch):
    """Layer 1 is reused rather than reimplemented, so the refusal is the server's own."""
    monkeypatch.setenv("DELEGATE_WORKSPACE_ROOTS", str(tmp_path / "permitted"))
    (tmp_path / "permitted").mkdir()
    (tmp_path / "elsewhere").mkdir()

    code, text = drive([str(tmp_path / "elsewhere")])

    assert code == 1
    assert "outside every workdir root" in text


@pytest.mark.skipif(os.name == "posix", reason="the refusal is for Windows specifically")
def test_it_refuses_on_windows_rather_than_building_something_unusable(capsys):
    """The venv it would build binds into a POSIX sandbox, so a Windows one is never usable."""
    code, _ = drive([PROJECT])
    assert code == 2
    assert "cannot run" in capsys.readouterr().err


# --- one real build ----------------------------------------------------------------------


@pytest.mark.integration
@posix_only
def test_a_real_build_leaves_a_usable_interpreter_and_a_record(tmp_path, monkeypatch):
    """The end of it: a real venv, a real record, and a digest matching the declaration."""
    root = project_tree(tmp_path, extras="")
    home = tmp_path / "home"
    monkeypatch.setenv("DELEGATE_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("DELEGATE_SANDBOX_HOME", str(home))

    code, text = drive([str(root)])

    assert code == 0, text
    venv = provision.venv_dir(str(home), str(root.resolve()))
    record = provision.read_record(venv)
    assert record is not None
    assert Path(record["interpreter"]).exists()
    assert record["dependency_hash"] == provision.dependency_hash(str(root.resolve()))
