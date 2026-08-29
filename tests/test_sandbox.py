"""The bubblewrap argv, and the refusal when bwrap is missing.

Almost everything here asserts `build_argv`'s output rather than running anything, which is
the point of the module's split: the bind ordering carries the security properties, it is
invisible until two paths overlap, and it must be provable on a machine with no `bwrap` --
including the Windows side, where Claude Code runs and where a contributor's first `pytest`
happens.

The cases that need a real sandbox are marked `integration` and live at the bottom. They
prove the properties argv assertions cannot: that the real HOME is absent rather than merely
unwritable, and that the network is denied. Network denial is checked **by address, never by
hostname** (ADR-0021) -- a hostname request fails whether or not the namespace is isolated,
so a hostname-only test would report a tight sandbox that might just have broken DNS.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import sandbox
from claude_delegate_local.config import Config
from claude_delegate_local.sandbox import SandboxRequest, SandboxUnavailable

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason=(
        "SANDBOX BEHAVIOUR UNPROVEN BY THIS RUN -- this is not a pass. These need a real "
        "bubblewrap, which exists in WSL and not on Windows. Run: wsl -d Ubuntu-24.04 -e "
        "bash -lc 'cd <repo> && ~/.venvs/delegate/bin/python -m pytest tests/test_sandbox.py'"
    ),
)

WORKDIR = "/mnt/c/Users/dev/proj"
HOME = "/home/dev/.cache/sandbox-home"


def cfg(**over) -> Config:
    kw = {"workspace_roots": (WORKDIR,)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def req(**over) -> SandboxRequest:
    kw = {"command": "echo hi", "home": HOME}
    kw.update(over)
    return SandboxRequest(**kw)  # type: ignore[arg-type]


def pairs(argv: list[str], flag: str) -> list[tuple[str, ...]]:
    """Every occurrence of `flag` with the arguments belonging to it.

    `--bind src dst` takes two, `--share-net` takes none. Written out rather than indexed by
    hand so a test reads as a claim about binds instead of about list offsets.
    """
    arity = {"--bind": 2, "--ro-bind": 2, "--symlink": 2, "--setenv": 2,
             "--dir": 1, "--chdir": 1, "--share-net": 0, "--clearenv": 0}[flag]
    out = []
    for i, tok in enumerate(argv):
        if tok == flag:
            out.append(tuple(argv[i + 1 : i + 1 + arity]))
    return out


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """A stand-in for `subprocess.run`'s result, for the cases that must not run bwrap."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def index_of(argv: list[str], flag: str, first_arg: str) -> int:
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv) and argv[i + 1] == first_arg:
            return i
    raise AssertionError(f"{flag} {first_arg} not in argv: {argv}")


# --- the corrected argv (ADR-0021) -------------------------------------------------------


def test_both_mandatory_symlinks_are_present():
    """Without lib64 nothing dynamically linked runs, and the error blames the executable.

    The failure this guards is not a missing feature but a misleading message: the kernel
    returns ENOENT for an absent ELF interpreter, so bwrap reports "No such file or
    directory" against a file that is present and readable.
    """
    links = pairs(sandbox.build_argv(cfg(), req()), "--symlink")
    assert ("usr/lib64", "/lib64") in links
    assert ("usr/sbin", "/sbin") in links


def test_the_root_is_empty_and_unshared():
    argv = sandbox.build_argv(cfg(), req())
    assert "--unshare-all" in argv
    assert "--die-with-parent" in argv
    assert ("/usr", "/usr") in pairs(argv, "--ro-bind")


def test_the_command_runs_through_sh_at_the_end():
    argv = sandbox.build_argv(cfg(), req(command="pytest -q"))
    assert argv[-4:] == ["--", "/bin/sh", "-c", "pytest -q"]


def test_the_configured_bwrap_binary_leads_the_argv():
    assert sandbox.build_argv(cfg(bwrap_bin="/opt/bwrap"), req())[0] == "/opt/bwrap"


# --- the three HOME/workdir cases --------------------------------------------------------


def test_case_one_no_workdir_binds_only_home_and_chdirs_there():
    """The shape every caller produces today: nothing supplies a workdir until M6."""
    argv = sandbox.build_argv(cfg(), req())
    assert (HOME, HOME) in pairs(argv, "--bind")
    assert pairs(argv, "--chdir") == [(HOME,)]
    assert len(pairs(argv, "--bind")) == 1


def test_case_two_a_distinct_workdir_is_bound_writable_and_becomes_the_cwd():
    argv = sandbox.build_argv(cfg(), req(workdir=WORKDIR))
    assert (WORKDIR, WORKDIR) in pairs(argv, "--bind")
    assert pairs(argv, "--chdir") == [(WORKDIR,)]


def test_case_three_a_workdir_inside_home_still_gets_its_own_later_bind():
    """Degenerate but legal, and the reason rule 1 is a rule.

    An operator can point a scratch project under the sandbox cache. Binding HOME first and
    the workdir second means the workdir keeps its own read-write bind rather than
    inheriting HOME's mode -- and asserting the order here is what stops that silently
    regressing if HOME's bind mode ever changes.
    """
    nested = f"{HOME}/proj"
    argv = sandbox.build_argv(cfg(), req(workdir=nested))
    assert index_of(argv, "--bind", HOME) < index_of(argv, "--bind", nested)
    assert pairs(argv, "--chdir") == [(nested,)]


def test_home_is_created_on_the_host_before_the_command_runs(tmp_path, monkeypatch):
    """Regression: `--dir` was used to make the HOME, and `--dir` makes it in the wrong place.

    bwrap creates a bind's mount point inside the sandbox on its own, but it cannot invent
    the source. Binding a HOME that does not exist on the host fails with "Can't find source
    path", which reads as a mistyped setting rather than as a directory nobody made yet --
    and on the very first run of a fresh install, nobody has.
    """
    home = tmp_path / "sandbox-home"
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: _completed())
    assert not home.exists()
    sandbox.run(cfg(), req(home=str(home)))
    assert home.is_dir()


def test_the_sandbox_home_setting_is_expanded_before_it_is_bound(monkeypatch):
    """bwrap binds paths, not shell words: a literal `~` names a directory called `~`."""
    monkeypatch.setenv("HOME", "/home/dev")
    resolved = sandbox.resolve_home(cfg(sandbox_home="~/.cache/delegate/home"))
    assert resolved == "/home/dev/.cache/delegate/home"
    assert "~" not in resolved


# --- bind order rule 2 -------------------------------------------------------------------


def test_readonly_toolchain_binds_come_before_the_writable_workdir():
    """An overlapping toolchain bind must not leave the workdir read-only.

    That failure surfaces as a read-only-filesystem error inside the directory the operator
    explicitly chose to build in, which names the wrong cause entirely.
    """
    argv = sandbox.build_argv(cfg(), req(workdir=WORKDIR, extra_binds=("/opt/uv",)))
    assert index_of(argv, "--ro-bind", "/opt/uv") < index_of(argv, "--bind", WORKDIR)


# --- network -----------------------------------------------------------------------------


def test_network_is_denied_by_default():
    argv = sandbox.build_argv(cfg(), req())
    assert "--share-net" not in argv


def test_asking_for_network_shares_it():
    assert "--share-net" in sandbox.build_argv(cfg(), req(network=True))


def test_resolv_conf_is_bound_at_its_real_path_only_when_the_network_is_shared(monkeypatch):
    """Binding /etc binds a dangling symlink under WSL, so DNS needs the target itself.

    The symptom that motivated this is specific: connections by address succeed while
    connections by name fail, which reads like a firewall rather than a mount.
    """
    monkeypatch.setattr(sandbox, "_resolv_conf_target", lambda: "/mnt/wsl/resolv.conf")
    shared = sandbox.build_argv(cfg(), req(network=True))
    assert ("/mnt/wsl/resolv.conf", "/mnt/wsl/resolv.conf") in pairs(shared, "--ro-bind")
    denied = sandbox.build_argv(cfg(), req())
    assert ("/mnt/wsl/resolv.conf", "/mnt/wsl/resolv.conf") not in pairs(denied, "--ro-bind")


# --- the environment ---------------------------------------------------------------------


def test_the_environment_is_cleared_and_home_points_inside_the_sandbox():
    argv = sandbox.build_argv(cfg(), req())
    assert "--clearenv" in argv
    assert ("HOME", HOME) in pairs(argv, "--setenv")
    assert ("PATH", sandbox.SANDBOX_PATH) in pairs(argv, "--setenv")


def test_passthrough_widens_the_allowlist_and_the_real_home_is_never_inherited(monkeypatch):
    """Inheriting the real HOME would undo the one property the empty root exists for."""
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    monkeypatch.setenv("CARGO_HOME", "/home/dev/.cargo")
    monkeypatch.setenv("HOME", "/home/dev")
    env = sandbox.resolve_env(cfg(env_passthrough=("CARGO_HOME", "HOME")))
    assert env["LANG"] == "en_GB.UTF-8"
    assert env["CARGO_HOME"] == "/home/dev/.cargo"
    assert "HOME" not in env


def test_an_unset_allowlisted_name_is_simply_absent(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    assert "TERM" not in sandbox.resolve_env(cfg())


# --- the toolchain probe -----------------------------------------------------------------


def test_a_configured_toolchain_list_wins_outright(monkeypatch):
    """Never merged with the probe: a configured value that gained an entry nobody wrote
    is exactly the config drift this project is built to prevent."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/uv")
    assert sandbox.probe_toolchain_binds(cfg(toolchain_binds=("/opt/tools",))) == ("/opt/tools",)


def test_uv_is_probed_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/home/dev/.local/bin/uv"
                        if name == "uv" else None)
    assert sandbox.probe_toolchain_binds(cfg()) == ("/home/dev/.local/bin/uv",)


def test_an_absent_uv_binds_nothing_rather_than_guessing(monkeypatch):
    """The documented first-run failure: `uv run pytest` reports 'not found' inside the
    sandbox. An accepted limitation, not a bug to paper over with a guessed path."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert sandbox.probe_toolchain_binds(cfg()) == ()


# --- the refusal -------------------------------------------------------------------------


def test_run_refuses_when_bwrap_is_absent(monkeypatch):
    """The negative test for the whole module: absent bwrap must refuse, never degrade."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(SandboxUnavailable):
        sandbox.run(cfg(), req())


def test_the_refusal_names_the_remedy(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(SandboxUnavailable, match="DELEGATE_BWRAP_BIN"):
        sandbox.run(cfg(), req())


def test_nothing_runs_the_command_when_bwrap_is_absent(monkeypatch):
    """Proves the refusal happens *instead of* the command, not alongside it.

    Asserting only that an exception came back would pass against an implementation that
    ran the command first and complained afterwards.
    """
    ran: list[object] = []
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(SandboxUnavailable):
        sandbox.run(cfg(), req())
    assert ran == []


def test_available_follows_the_configured_binary_name(monkeypatch):
    seen: list[str] = []

    def fake_which(name):
        seen.append(name)
        return "/opt/bwrap" if name == "/opt/bwrap" else None

    monkeypatch.setattr(shutil, "which", fake_which)
    assert sandbox.available(cfg(bwrap_bin="/opt/bwrap")) is True
    assert sandbox.available(cfg(bwrap_bin="bwrap")) is False
    assert seen == ["/opt/bwrap", "bwrap"]


# --- against a real bubblewrap -----------------------------------------------------------


@pytest.mark.integration
@needs_bwrap
def test_a_command_runs_and_its_real_exit_code_is_captured(tmp_path):
    """Server-captured, from a real process exit -- the ground truth ADR-0007 rests on."""
    home = str(tmp_path / "home")
    ok = sandbox.run(cfg(), req(command="echo hello", home=home))
    assert ok.exit_code == 0
    assert ok.stdout.strip() == "hello"
    assert ok.timed_out is False

    bad = sandbox.run(cfg(), req(command="exit 3", home=home))
    assert bad.exit_code == 3


@pytest.mark.integration
@needs_bwrap
def test_the_real_home_is_absent_rather_than_merely_unwritable(tmp_path):
    """Absent, not read-only. A read-only ~/.ssh is still a readable ~/.ssh."""
    home = str(tmp_path / "home")
    result = sandbox.run(cfg(), req(command="ls -d ~/.ssh 2>&1 || true", home=home))
    assert ".ssh" not in result.stdout or "No such file" in result.stdout


@pytest.mark.integration
@needs_bwrap
def test_network_is_denied_by_address(tmp_path):
    """By address, never by hostname (ADR-0021).

    A hostname lookup fails whether or not the namespace is isolated, so a hostname-based
    test would pass against a sandbox with network access and broken DNS -- reporting a
    control that is not there. curl exits 7 (connect failed) and prints 000 when denied.
    """
    home = str(tmp_path / "home")
    denied = sandbox.run(cfg(), req(
        command='curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://1.1.1.1',
        home=home))
    assert denied.stdout.strip() == "000"


@pytest.mark.integration
@needs_bwrap
def test_network_is_reachable_by_address_when_shared(tmp_path):
    """The other half. Without this, the denial test above could pass on a broken curl."""
    home = str(tmp_path / "home")
    shared = sandbox.run(cfg(), req(
        command='curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://1.1.1.1',
        home=home, network=True))
    assert shared.stdout.strip() not in ("", "000")


@pytest.mark.integration
@needs_bwrap
def test_a_bound_workdir_is_writable(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = sandbox.run(cfg(), req(
        command="echo written > proof.txt", home=str(tmp_path / "home"),
        workdir=str(work)))
    assert result.exit_code == 0
    assert (work / "proof.txt").read_text().strip() == "written"


@pytest.mark.integration
@needs_bwrap
def test_a_hanging_command_is_killed_and_reported_as_a_timeout(tmp_path):
    """`None` rather than a number: a real 124 is a command's own choice, not a timeout."""
    result = sandbox.run(cfg(run_bash_timeout=2), req(
        command="sleep 30", home=str(tmp_path / "home")))
    assert result.timed_out is True
    assert result.exit_code is None
