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


def test_the_same_declaration_in_crlf_and_lf_hashes_the_same(tmp_path):
    """The bug: a line-ending flip read as a changed dependency declaration.

    Measured 2026-09-08 on this repository. The recorded hash was of the CRLF form, a
    `git reset --hard` rewrote the file as LF, and `--doctor` failed while
    `interpreter_for` withheld the interpreter -- so `run_bash` could run no test at all,
    with nothing about the project changed. On a Windows checkout that is every branch
    switch, not an edge case.
    """
    root = tmp_path / "proj"
    root.mkdir()
    body = '[project]\nname = "demo"\nversion = "0.0.0"\n'

    (root / "pyproject.toml").write_bytes(body.encode())
    lf = provision.dependency_hash(str(root))
    (root / "pyproject.toml").write_bytes(body.replace("\n", "\r\n").encode())
    crlf = provision.dependency_hash(str(root))

    assert lf == crlf


def test_a_real_dependency_edit_still_moves_the_digest(tmp_path):
    """The control, and the half that must not be weakened by the fix above.

    A digest that ignored everything would satisfy the CRLF test perfectly. Stale
    dependencies do not error -- they pass against the wrong versions and return 0, the one
    number ADR-0007 says to believe -- so this direction is the one with teeth.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_bytes(
        b'[project]\nname = "demo"\ndependencies = ["httpx>=0.28"]\n')
    before = provision.dependency_hash(str(root))
    (root / "pyproject.toml").write_bytes(
        b'[project]\nname = "demo"\ndependencies = ["httpx>=0.29"]\n')

    assert provision.dependency_hash(str(root)) != before


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
    """Reported, never covered. Covering is what breaks the environment (ADR-0041).

    The tree is *not* called `venv`, and that is not cosmetic. `security/opaque_globs.txt`
    names `venv/**`, so a fixture called that has its own contents pruned by this walk and
    the planted file is never reached -- which made the clean-tree control below pass for
    the wrong reason until both were renamed. The real provisioned root is `venvs/<name>`,
    which the pattern does not match.
    """
    site = tmp_path / "prov" / "lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "credentials.py").write_text("x", encoding="utf-8")
    (site / "ordinary.py").write_text("x", encoding="utf-8")

    found = provision.denylist_matches(cfg(), str(tmp_path / "prov"))

    assert [Path(p).name for p in found] == ["credentials.py"]


@posix_only
def test_a_clean_tree_reports_nothing(tmp_path):
    """The control, so the reporter cannot pass by matching everything or nothing."""
    site = tmp_path / "prov" / "lib"
    site.mkdir(parents=True)
    (site / "ordinary.py").write_text("x", encoding="utf-8")
    assert provision.denylist_matches(cfg(), str(tmp_path / "prov")) == []


@posix_only
def test_an_opaque_directory_is_pruned_so_the_count_matches_the_scans(tmp_path):
    """The compiled twin of a match must not be counted a second time.

    Without pruning, this reported 44 on this repository where the scan itself would have
    covered 13 -- every `__pycache__` descended into and every `.pyc` of a matched name
    counted. The opaque list is asked with the same directory probe the scan uses, since
    `__pycache__/**` matches nothing against the directory's own path.
    """
    pkg = tmp_path / "prov" / "lib" / "pkg"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "credentials.py").write_text("x", encoding="utf-8")
    (pkg / "__pycache__" / "credentials.cpython-312.pyc").write_text("x", encoding="utf-8")

    found = provision.denylist_matches(cfg(), str(tmp_path / "prov"))

    assert [Path(p).name for p in found] == ["credentials.py"]


# --- handing the interpreter to run_bash -------------------------------------------------


def recorded(home: Path, project: Path, **over) -> Path:
    """One provisioned environment, shaped as `provision` leaves it, without a real build."""
    venv = Path(provision.venv_dir(home.as_posix(), project.as_posix()))
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    record = {
        "project": project.as_posix(),
        "interpreter": (venv / "bin" / "python").as_posix(),
        "dependency_hash": provision.dependency_hash(project.as_posix()),
        "hash_source": provision.HASH_SOURCE,
    }
    record.update(over)
    (venv / provision.RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    return venv


@posix_only
def test_the_interpreter_is_found_for_the_project_it_was_built_for(tmp_path):
    home = tmp_path / "home"
    project = project_tree(tmp_path, extras="")
    venv = recorded(home, project)

    found = provision.interpreter_for(cfg(sandbox_home=str(home)), project.as_posix())

    assert found == (venv / "bin" / "python").as_posix()


@posix_only
def test_a_workdir_inside_the_project_still_finds_it(tmp_path):
    """An editable install works from anywhere under the project, so a subdirectory counts."""
    home = tmp_path / "home"
    project = project_tree(tmp_path, extras="")
    (project / "src").mkdir()
    recorded(home, project)

    assert provision.interpreter_for(
        cfg(sandbox_home=str(home)), (project / "src").as_posix()
    ) is not None


@posix_only
def test_an_unrelated_workdir_finds_nothing(tmp_path):
    """The control. A lookup that matched anything would hand one project another's python."""
    home = tmp_path / "home"
    recorded(home, project_tree(tmp_path, extras=""))
    other = tmp_path / "elsewhere"
    other.mkdir()

    assert provision.interpreter_for(cfg(sandbox_home=str(home)), other.as_posix()) is None


@posix_only
def test_a_stale_environment_is_withheld_rather_than_offered(tmp_path):
    """The point of asking `is_current` instead of just looking.

    Offering an interpreter built from a declaration that has moved on is how a delegation
    reports a passing suite at exit 0 against the wrong versions -- and exit 0 is the one
    number ADR-0007 tells everything downstream to believe. Absent is a state the model can
    report and `--doctor` explains; a false pass is neither.
    """
    home = tmp_path / "home"
    project = project_tree(tmp_path, extras="")
    recorded(home, project)
    (project / "pyproject.toml").write_text('[project]\nname = "moved-on"\n', encoding="utf-8")

    assert provision.interpreter_for(cfg(sandbox_home=str(home)), project.as_posix()) is None


@posix_only
def test_a_vanished_interpreter_is_withheld(tmp_path):
    home = tmp_path / "home"
    project = project_tree(tmp_path, extras="")
    venv = recorded(home, project)
    (venv / "bin" / "python").unlink()

    assert provision.interpreter_for(cfg(sandbox_home=str(home)), project.as_posix()) is None


@posix_only
def test_the_nearest_project_wins_for_a_nested_checkout(tmp_path):
    """Longest match, so a checkout inside another is not served its parent's environment."""
    home = tmp_path / "home"
    outer = project_tree(tmp_path, extras="")
    inner = outer / "vendored"
    inner.mkdir()
    (inner / "pyproject.toml").write_text('[project]\nname = "inner"\n', encoding="utf-8")
    recorded(home, outer)
    inner_venv = recorded(home, inner)

    found = provision.interpreter_for(cfg(sandbox_home=str(home)), inner.as_posix())

    assert found == (inner_venv / "bin" / "python").as_posix()


def test_no_workdir_means_no_interpreter():
    """`workdir` is None for a delegation that named none, and there is nothing to match."""
    assert provision.interpreter_for(cfg(), None) is None


@posix_only
def test_the_name_is_absent_rather_than_empty_when_nothing_is_provisioned(tmp_path):
    """A shell expands an unset name to nothing, so `$DELEGATE_PYTHON -m pytest` would run
    `-m pytest` as a command and fail for a reason unrelated to the actual cause."""
    env = provision.sandbox_env(cfg(sandbox_home=str(tmp_path / "home")), str(tmp_path))
    assert env == {}


@posix_only
def test_the_name_carries_the_absolute_path_when_one_is_current(tmp_path):
    home = tmp_path / "home"
    project = project_tree(tmp_path, extras="")
    recorded(home, project)

    env = provision.sandbox_env(cfg(sandbox_home=str(home)), project.as_posix())

    assert list(env) == [provision.SANDBOX_ENV_NAME]
    assert env[provision.SANDBOX_ENV_NAME].startswith("/")


# --- tests that cannot run nested --------------------------------------------------------


def with_deselect(tmp_path: Path, *nodes: str) -> Path:
    """A project declaring its own nested-exclusion list."""
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    listed = "".join(f'    "{n}",\n' for n in nodes)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n'
        "[tool.delegate-local]\nnested-deselect = [\n" + listed + "]\n",
        encoding="utf-8",
    )
    return root


def test_the_declared_list_is_read(tmp_path):
    root = with_deselect(tmp_path, "tests/test_a.py::test_one", "tests/test_b.py::test_two")
    assert provision.nested_deselect(str(root)) == (
        "tests/test_a.py::test_one", "tests/test_b.py::test_two")


def test_a_project_declaring_nothing_has_an_empty_list(tmp_path):
    """The control: a reader returning something for every project would deselect blindly."""
    assert provision.nested_deselect(str(project_tree(tmp_path))) == ()


def test_a_malformed_table_is_empty_rather_than_an_error(tmp_path):
    """This runs on the path of every run_bash call. Refusing a shell command over a
    misspelt table would be a far worse failure than running the test that cannot pass."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "d"\n\n[tool.delegate-local]\nnested-deselect = "oops"\n',
        encoding="utf-8",
    )
    assert provision.nested_deselect(str(root)) == ()


def test_each_node_becomes_its_own_deselect():
    got = provision.addopts_for(["tests/a.py::test_x", "tests/b.py::test_y"])
    assert got == "--deselect tests/a.py::test_x --deselect tests/b.py::test_y"


def test_a_node_id_carrying_a_space_is_quoted():
    """pytest splits PYTEST_ADDOPTS the way a shell would, and a parametrised case can carry
    a space -- unquoted it would arrive as two arguments."""
    assert provision.addopts_for(["tests/a.py::test_x[a b]"]) == (
        "--deselect 'tests/a.py::test_x[a b]'")


def test_an_empty_list_produces_no_addopts():
    """Empty rather than a bare `--deselect`, which pytest would refuse outright."""
    assert provision.addopts_for([]) == ""


@posix_only
def test_the_deselects_reach_the_sandbox_beside_the_interpreter(tmp_path):
    home = tmp_path / "home"
    project = with_deselect(tmp_path, "tests/test_sandbox.py::test_needs_network")
    recorded(home, project)

    env = provision.sandbox_env(cfg(sandbox_home=str(home)), project.as_posix())

    assert env["PYTEST_ADDOPTS"] == "--deselect tests/test_sandbox.py::test_needs_network"
    assert env[provision.SANDBOX_ENV_NAME].endswith("/bin/python")


@posix_only
def test_a_project_with_no_list_gets_no_addopts_name_at_all(tmp_path):
    """Absent rather than empty, for the same reason the interpreter name is."""
    home = tmp_path / "home"
    recorded(home, project_tree(tmp_path, extras=""))
    project = tmp_path / "proj"

    env = provision.sandbox_env(cfg(sandbox_home=str(home)), project.as_posix())

    assert "PYTEST_ADDOPTS" not in env
    assert provision.SANDBOX_ENV_NAME in env


@posix_only
def test_a_stale_environment_carries_no_deselects_either(tmp_path):
    """They travel with the interpreter: withholding one and offering the other would
    deselect tests for a command that has no interpreter to run them with."""
    home = tmp_path / "home"
    project = with_deselect(tmp_path, "tests/test_a.py::test_one")
    recorded(home, project)
    (project / "pyproject.toml").write_text('[project]\nname = "moved"\n', encoding="utf-8")

    assert provision.sandbox_env(cfg(sandbox_home=str(home)), project.as_posix()) == {}


def test_this_repository_declares_the_tests_that_cannot_run_nested():
    """Measured: every one of these needs the network `--unshare-all` denies.

    Asserted against the shipped `pyproject.toml` rather than a fixture, so deleting an
    entry fails here instead of surfacing as a false failure inside a delegation. The list
    grew from one to six on 2026-09-09, when this suite was first actually run nested --
    four talk to the live endpoint and one runs a real `pip install`, and all five pass on
    the host, which is why nothing had noticed.
    """
    listed = provision.nested_deselect(str(Path(__file__).resolve().parents[1]))
    for node in (
        "tests/test_sandbox.py::test_network_is_reachable_by_address_when_shared",
        "tests/test_backends_openai_compat.py::test_probe_against_the_live_endpoint",
        "tests/test_backends_openai_compat.py::test_one_real_completion_against_the_live_endpoint",
        ("tests/regression/test_reasoning_exhaustion_recovers.py"
         "::test_the_live_empty_answer_is_recovered_by_the_budget_retry"),
        ("tests/regression/test_reasoning_exhaustion_recovers.py"
         "::test_the_live_verdict_is_exhaustion_only_after_the_step_down_was_tried"),
        "tests/test_provision.py::test_a_real_build_leaves_a_usable_interpreter_and_a_record",
    ):
        assert node in listed, f"{node} is no longer declared un-nestable"


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
