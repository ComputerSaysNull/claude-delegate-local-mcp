"""The preflight checks, and the proof that each one can fail.

Every check here is asserted in both directions. That is not thoroughness for its own
sake: this repository has already found four checks that could not fail, and a doctor is
the worst possible place for a fifth -- it is read precisely when someone has stopped
trusting their own environment, so a check that always passes would send them looking
somewhere else. Each test that asserts a `PASS` has a sibling that breaks the one
condition and asserts the `FAIL`.

The endpoint probe is exercised against a closed loopback port rather than mocked. A mock
would assert that this module reads a dict correctly, which is not the thing that breaks;
what breaks is the probe's own error handling, and that is `server.probe_entry`'s and is
reused here on purpose.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import doctor, provision, sandbox
from claude_delegate_local.config import Config
from claude_delegate_local.doctor import FAIL, OK, WARN, Check
from claude_delegate_local.registry import ModelEntry, Registry

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "DOCTOR BEHAVIOUR UNPROVEN BY THIS RUN -- not a pass. These probe real mounts, real "
        "permissions, and roots resolved through `to_posix`, which on Windows yields a "
        "drive-letter path that cannot exist. The doctor refuses to run outside POSIX before "
        "reaching any of them. Run: wsl -d Ubuntu-24.04 -e bash -lc 'cd <repo> && "
        "/tmp/vv/bin/python -m pytest tests/test_doctor.py'"
    ),
)


def cfg(**over) -> Config:
    kw = {"workspace_roots": (str(Path.cwd()),)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def closed_port() -> int:
    """A port nothing is listening on: bind, read the number, release it.

    Better than a hardcoded number, which is only probably closed and fails oddly on the
    machine where it is not.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def one_entry_registry(port: int) -> Registry:
    entry = ModelEntry(
        key="probe",
        base_url=f"http://127.0.0.1:{port}",
        served_model_id="probe-model",
    )
    return Registry(entries={"probe": entry}, default_key="probe")


# --- workspace roots ----------------------------------------------------------------


@posix_only
def test_present_roots_pass(tmp_path):
    assert doctor.check_workspace_roots(cfg(workspace_roots=(str(tmp_path),))).verdict == OK


@posix_only
def test_missing_root_fails(tmp_path):
    """The negative half. config.load never asks whether a root exists."""
    absent = tmp_path / "not-created"
    check = doctor.check_workspace_roots(cfg(workspace_roots=(str(absent),)))
    assert check.verdict == FAIL
    assert "missing" in check.detail
    assert str(absent) in check.extra["missing"][0]


@posix_only
def test_one_missing_root_among_present_ones_still_fails(tmp_path):
    """A partial failure must not average out to a pass."""
    good = tmp_path / "good"
    good.mkdir()
    check = doctor.check_workspace_roots(
        cfg(workspace_roots=(str(good), str(tmp_path / "absent")))
    )
    assert check.verdict == FAIL
    assert "1 of 2" in check.detail


# --- bwrap --------------------------------------------------------------------------


def test_missing_bwrap_fails():
    check = doctor.check_bwrap(cfg(bwrap_bin="definitely-not-a-real-binary-name"))
    assert check.verdict == FAIL
    assert "not on PATH" in check.detail


@posix_only
def test_bwrap_present_but_failing_is_still_a_fail(tmp_path):
    """The case `sandbox.available` cannot see.

    A bubblewrap that is installed and cannot unshare a namespace is on PATH, so the
    availability helper says yes and every `run_bash` afterwards is refused. The doctor
    runs the binary rather than looking it up, and this fake proves the difference: it
    exists, it is executable, and it exits non-zero.
    """
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: cannot unshare' >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    c = cfg(bwrap_bin=str(fake))
    assert sandbox.available(c) is True, "the availability helper should be satisfied"
    check = doctor.check_bwrap(c)
    assert check.verdict == FAIL
    assert "cannot unshare" in check.detail


@posix_only
@pytest.mark.integration
@pytest.mark.skipif(
    not sandbox.available(Config(workspace_roots=("/tmp",))),  # type: ignore[arg-type]
    reason=(
        "BWRAP EXECUTION UNPROVEN BY THIS RUN -- not a pass. `available()` is a PATH lookup "
        "and proves nothing about unsharing, which is the doctor's whole point, so this is "
        "`integration`-marked as well: the CI runner installs bubblewrap and still cannot "
        "bring up loopback in a new network namespace (no CAP_NET_ADMIN), where the probe's "
        "`--unshare-all` correctly reports FAIL because `run_bash` would also fail there. "
        "Run: wsl -d Ubuntu-24.04 -e bash -lc 'cd <repo> && /tmp/vv/bin/python -m pytest "
        "tests/test_doctor.py -m integration'"
    ),
)
def test_real_bwrap_passes():
    assert doctor.check_bwrap(cfg()).verdict == OK


# --- toolchain ----------------------------------------------------------------------


def test_configured_toolchain_passes(tmp_path):
    binary = tmp_path / "tool"
    binary.write_text("", encoding="utf-8")
    check = doctor.check_toolchain(cfg(toolchain_binds=(str(binary),)))
    assert check.verdict == OK
    assert "configured" in check.detail


def test_nothing_bound_warns_rather_than_fails(monkeypatch):
    """A read-only delegation needs no toolchain, so this must not block."""
    monkeypatch.setattr(sandbox, "probe_toolchain_binds", lambda _cfg: ())
    check = doctor.check_toolchain(cfg())
    assert check.verdict == WARN
    assert doctor.worst([check]) == WARN


# --- provisioned environments -------------------------------------------------------


def provisioned(home: Path, project: Path, **over) -> Path:
    """A provisioned environment shaped the way `provision` leaves one, without building it.

    Paths go in POSIX-shaped, which is what production hands `provision`: `resolve_workdir`
    translates and resolves before anything here sees a value, and `venv_name` takes a
    POSIX basename deliberately -- fed a Windows path it would return the whole string as
    the directory name.
    """
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


def declared(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "d"\n', encoding="utf-8")
    return project


@posix_only
def test_nothing_provisioned_warns_rather_than_fails(tmp_path):
    """The read-heavy majority of delegations needs no interpreter, so this must not block.

    Same call as `check_toolchain` makes, and for the same reason.
    """
    checks = doctor.check_provisioned(cfg(sandbox_home=str(tmp_path / "home")))
    assert [c.verdict for c in checks] == [WARN]
    assert doctor.worst(checks) == WARN


@posix_only
def test_a_current_environment_passes(tmp_path):
    home = tmp_path / "home"
    provisioned(home, declared(tmp_path))
    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))
    assert [c.verdict for c in checks] == [OK]


@posix_only
def test_a_stale_declaration_fails(tmp_path):
    """A FAIL and not a WARN, which is the opposite call to the absence case above.

    Stale dependencies do not error. They produce a passing test run against the wrong
    versions and hand back exit 0 -- the one number ADR-0007 says to believe -- so this is
    worse than having no environment at all, because it is believed.
    """
    home = tmp_path / "home"
    project = declared(tmp_path)
    provisioned(home, project)
    (project / "pyproject.toml").write_text('[project]\nname = "moved-on"\n', encoding="utf-8")

    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))

    assert [c.verdict for c in checks] == [FAIL]
    assert doctor.worst(checks) == FAIL


@posix_only
def test_a_missing_record_fails(tmp_path):
    """A half-built environment is exactly where a test run passes against nothing."""
    home = tmp_path / "home"
    Path(provision.venv_dir(home.as_posix(), (tmp_path / "proj").as_posix())).mkdir(
        parents=True
    )
    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))
    assert [c.verdict for c in checks] == [FAIL]


@posix_only
def test_a_vanished_interpreter_fails(tmp_path):
    home = tmp_path / "home"
    venv = provisioned(home, declared(tmp_path))
    (venv / "bin" / "python").unlink()
    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))
    assert [c.verdict for c in checks] == [FAIL]


@posix_only
def test_a_project_that_has_gone_away_fails(tmp_path):
    home = tmp_path / "home"
    project = declared(tmp_path)
    provisioned(home, project)
    (project / "pyproject.toml").unlink()
    checks = doctor.check_provisioned(cfg(sandbox_home=str(home)))
    assert [c.verdict for c in checks] == [FAIL]


@posix_only
def test_an_uncovered_denylist_match_is_reported_as_a_warning(tmp_path):
    """Nothing covers this tree at runtime, so the doctor is where the count is reported.

    ADR-0062 moves the guarantee to build time, and a guarantee nothing re-checks stops
    being true quietly. Ordinary library filenames match, so it is a count and not a
    refusal.
    """
    home = tmp_path / "home"
    venv = provisioned(home, declared(tmp_path))
    (venv / "credentials.py").write_text("", encoding="utf-8")

    check = doctor.check_provisioned_secrets(cfg(sandbox_home=str(home)))

    assert check.verdict == WARN
    assert doctor.worst([check]) == WARN


@posix_only
def test_a_clean_provisioned_tree_passes(tmp_path):
    """The control: without it the warning above could fire on any tree at all."""
    home = tmp_path / "home"
    provisioned(home, declared(tmp_path))
    check = doctor.check_provisioned_secrets(cfg(sandbox_home=str(home)))
    assert check.verdict == OK


@posix_only
def test_nothing_provisioned_is_not_reported_as_uncovered(tmp_path):
    check = doctor.check_provisioned_secrets(cfg(sandbox_home=str(tmp_path / "home")))
    assert check.verdict == OK


# --- transcripts --------------------------------------------------------------------


def test_transcripts_off_warns():
    assert doctor.check_transcripts(cfg(transcript_dir="")).verdict == WARN


def test_writable_transcript_dir_passes(tmp_path):
    check = doctor.check_transcripts(cfg(transcript_dir=str(tmp_path / "records")))
    assert check.verdict == OK


@posix_only
def test_unwritable_transcript_dir_fails(tmp_path):
    """Runtime swallows a transcript failure by design, so this is the only report of it."""
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    check = doctor.check_transcripts(cfg(transcript_dir=str(blocker / "under-a-file")))
    assert check.verdict == FAIL
    assert "not writable" in check.detail


# --- cross-process slots ------------------------------------------------------------


def test_disabled_cross_process_warns():
    check = doctor.check_cross_process(cfg(cross_process_slots=False))
    assert check.verdict == WARN
    assert "DELEGATE_CROSS_PROCESS_SLOTS" in check.detail


# --- endpoints ----------------------------------------------------------------------


def test_dead_endpoint_fails():
    """Reuses server.probe_entry, so this also pins that a dead endpoint is data."""
    checks = doctor.check_endpoints(cfg(), one_entry_registry(closed_port()))
    assert len(checks) == 1
    assert checks[0].verdict == FAIL
    assert checks[0].name == "endpoint:probe"


def test_empty_registry_fails():
    checks = doctor.check_endpoints(cfg(), Registry(entries={}, default_key=""))
    assert [c.verdict for c in checks] == [FAIL]


def test_endpoint_check_never_names_the_address():
    """ADR-0029 keeps the endpoint out of backend_status; a diagnostic must not leak it."""
    port = closed_port()
    checks = doctor.check_endpoints(cfg(), one_entry_registry(port))
    rendered = " ".join(c.detail + c.remedy for c in checks)
    assert "127.0.0.1" not in rendered
    assert str(port) not in rendered


# --- platform -----------------------------------------------------------------------


def test_non_posix_platform_fails(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "nt")
    check = doctor.check_platform()
    assert check.verdict == FAIL
    assert "inside WSL" in check.remedy


@posix_only
def test_posix_platform_passes():
    assert doctor.check_platform().verdict == OK


# --- verdicts and exit code ---------------------------------------------------------


def test_worst_ranks_fail_above_warn_above_ok():
    assert doctor.worst([Check("a", OK, "")]) == OK
    assert doctor.worst([Check("a", OK, ""), Check("b", WARN, "")]) == WARN
    assert doctor.worst([Check("a", WARN, ""), Check("b", FAIL, "")]) == FAIL
    assert doctor.worst([]) == OK


def test_exit_code_is_zero_for_warnings_and_non_zero_for_failures(capsys):
    assert doctor.report([Check("a", OK, "fine"), Check("b", WARN, "noted")]) == 0
    assert "PASS (1 warning(s))" in capsys.readouterr().out

    assert doctor.report([Check("a", FAIL, "broken")]) == 1
    out = capsys.readouterr().out
    assert "FAIL: 1 blocking" in out


def test_report_prints_the_remedy_only_for_a_failing_check(capsys):
    doctor.report([
        Check("good", OK, "fine", remedy="should not appear"),
        Check("bad", FAIL, "broken", remedy="should appear"),
    ])
    out = capsys.readouterr().out
    assert "should appear" in out
    assert "should not appear" not in out


# --- the entry point ----------------------------------------------------------------


def test_doctor_flag_short_circuits_the_server(monkeypatch):
    """`--doctor` must return before the server is built, not alongside it."""
    from claude_delegate_local import main as main_mod

    monkeypatch.setattr(sys, "argv", ["claude-delegate-local-mcp", "--doctor"])
    monkeypatch.setattr(doctor, "main", lambda: 7)

    def explode(*_a, **_k):  # pragma: no cover -- reaching this is the failure
        raise AssertionError("the server was built during a doctor run")

    monkeypatch.setattr(main_mod.server, "build", explode)
    monkeypatch.setattr(main_mod.config, "load", explode)

    with pytest.raises(SystemExit) as exc:
        main_mod.run()
    assert exc.value.code == 7


def test_without_the_flag_the_doctor_does_not_run(monkeypatch):
    """The negative half: an ordinary launch must not print a report onto the protocol."""
    from claude_delegate_local import main as main_mod

    monkeypatch.setattr(sys, "argv", ["claude-delegate-local-mcp"])

    def explode():  # pragma: no cover -- reaching this is the failure
        raise AssertionError("the doctor ran on an ordinary launch")

    monkeypatch.setattr(doctor, "main", explode)
    monkeypatch.setattr(
        main_mod.config, "load",
        lambda: (_ for _ in ()).throw(main_mod.config.ConfigError("stop here")),
    )
    with pytest.raises(SystemExit) as exc:
        main_mod.run()
    assert exc.value.code == 1
