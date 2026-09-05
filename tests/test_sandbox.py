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

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import sandbox
from claude_delegate_local.config import Config
from claude_delegate_local.paths import PathPolicyError
from claude_delegate_local.sandbox import SandboxRequest, SandboxUnavailable

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason=(
        "SANDBOX BEHAVIOUR UNPROVEN BY THIS RUN -- this is not a pass. These need a real "
        "bubblewrap, which exists in WSL and not on Windows. Run: wsl -d Ubuntu-24.04 -e "
        "bash -lc 'cd <repo> && ~/.venvs/delegate/bin/python -m pytest tests/test_sandbox.py'"
    ),
)

SCAN_UNPROVEN = (
    "SECRET SCAN UNPROVEN BY THIS RUN -- this is not a pass. The walk matches denylist "
    "patterns against POSIX path suffixes, and os.walk yields backslashes on Windows, so "
    "it finds nothing here rather than failing. Run it in WSL -- see CONTRIBUTING.md."
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=SCAN_UNPROVEN)

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
             "--dir": 1, "--chdir": 1, "--tmpfs": 1, "--share-net": 0, "--clearenv": 0}[flag]
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


def test_the_configured_bwrap_binary_leads_the_sandbox_argv():
    """It no longer leads the whole argv: since 2026-09-05 a `prlimit` launcher runs in
    front of it, because bwrap 0.9.0 has no --rlimit of its own. With the limits off, bwrap
    is first again, and both halves are asserted so neither can drift unnoticed."""
    c = cfg(bwrap_bin="/opt/bwrap")
    argv = sandbox.build_argv(c, req())
    assert argv[argv.index("--") + 1] == "/opt/bwrap"

    unlimited = cfg(bwrap_bin="/opt/bwrap", sandbox_max_memory_mb=0,
                    sandbox_max_file_mb=0, sandbox_max_processes=0)
    assert sandbox.build_argv(unlimited, req())[0] == "/opt/bwrap"


# --- the three HOME/workdir cases --------------------------------------------------------


def test_case_one_no_workdir_binds_only_home_and_chdirs_there():
    """The shape a delegation gets when it names no workdir, which is still the default."""
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


# --- bind order rule 2a ------------------------------------------------------------------


def base_mount_block(argv: list[str], c: Config) -> int:
    """Where the base mount block starts in `argv`.

    Anchored on the whole contiguous block rather than on a single flag, and that is the
    point of it. An agent binding /usr emits `--ro-bind /usr /usr`, which is byte-identical
    to the base mount of /usr -- so an assertion phrased as "the first --ro-bind /usr comes
    before the last mention of /usr" is true whichever order they are in, and would pass
    against the very bug these tests exist for. Ask where the block is instead.

    Takes the config because `--size` is spliced in ahead of the tmpfs when one is set, so
    the emitted block is no longer the static table.
    """
    block = sandbox._base_mounts_argv(c)
    for i in range(len(argv) - len(block) + 1):
        if argv[i:i + len(block)] == block:
            return i
    raise AssertionError(f"the base mount block is not in argv: {argv}")


@pytest.mark.parametrize("target", sorted(sandbox.base_mount_targets()))
def test_an_extra_bind_inside_a_base_mount_is_applied_after_it(target):
    """Rule 2a, and the reason the order is this way round rather than the other.

    /tmp is a tmpfs: a bind under it emitted *before* the base mounts is wiped, and on Linux
    every temporary directory is under /tmp -- which is what an integration test found on
    2026-09-05 when the order was inverted to stop a bind shadowing /usr. So the shadowing
    case is refused in `agents.py` instead, and this asserts the ordering that refusal
    assumes. Parametrised over the derived table because a mount added later is exactly the
    case a hand-written list would miss.
    """
    inside = f"{target.rstrip('/')}/toolchain"
    c = cfg()
    argv = sandbox.build_argv(c, req(extra_binds=(inside,)))
    assert index_of(argv, "--ro-bind", inside) > base_mount_block(argv, c), (
        f"a bind under {target} is applied before the sandbox mounts {target}, so it is lost"
    )


def test_an_extra_bind_under_home_is_applied_after_home():
    """The same, for HOME, which is a config value rather than part of the base table."""
    inside = f"{HOME}/toolchain"
    argv = sandbox.build_argv(cfg(), req(extra_binds=(inside,)))
    assert index_of(argv, "--ro-bind", inside) > index_of(argv, "--bind", HOME)


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
def test_a_secret_inside_an_extra_bind_cannot_be_read(tmp_path):
    """Read it and get nothing, rather than assert the mount op is in the argv.

    #36's lesson, applied to the bind this commit opened: asserting a flag is present proves
    it was passed, not that anything acts on it. An agent file chooses `extra_binds` now, so
    the question is not whether a shadow was requested but whether a shell inside the
    sandbox can still read the key -- and that is answerable only by trying.
    """
    home = tmp_path / "home"
    home.mkdir()
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    (toolchain / "id_rsa").write_text("PRIVATE KEY MATERIAL", encoding="utf-8")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(command=f"cat {toolchain}/id_rsa 2>&1 || true",
            home=str(home), extra_binds=(str(toolchain),)))

    assert "PRIVATE KEY MATERIAL" not in result.stdout


@pytest.mark.integration
@needs_bwrap
def test_a_non_secret_inside_an_extra_bind_is_still_readable(tmp_path):
    """The other half. Without it, a bind that failed outright would satisfy the test above
    while breaking the feature it is meant to protect."""
    home = tmp_path / "home"
    home.mkdir()
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    (toolchain / "runner.sh").write_text("ORDINARY TOOLCHAIN FILE", encoding="utf-8")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(command=f"cat {toolchain}/runner.sh", home=str(home),
            extra_binds=(str(toolchain),)))

    assert "ORDINARY TOOLCHAIN FILE" in result.stdout


@pytest.mark.integration
@needs_bwrap
def test_a_bound_workdir_is_the_working_directory_and_is_writable(tmp_path):
    """What binding a workspace is for. Until this commit `workdir` was None for every
    caller, so a delegated model could not reach a repository at all."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "proj"
    work.mkdir()

    result = sandbox.run(cfg(), req(
        command="pwd && echo written > made-here.txt", home=str(home), workdir=str(work)))

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines()[0] == str(work)
    assert (work / "made-here.txt").read_text(encoding="utf-8").strip() == "written"


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


def _orphaned_run(argv: list[str], workdir: Path, *, linger: float = 2.0) -> None:
    """Start `argv` from a parent that exits while the sandboxed command is still running.

    `sandbox.run` blocks until the command finishes, so it cannot express this at all: the
    parent whose death matters is the server process, and a test cannot kill its own
    interpreter. So the parent here is a throwaway shell that backgrounds bwrap and then
    exits on its own -- no signal is sent to anything, which matters, because
    `start_new_session=True` puts the sandbox in its own process group and a test that
    killed the group would be proving its own teardown rather than `--die-with-parent`.

    The shell lingers briefly before exiting. Without that, it can exit before bwrap has
    installed its parent-death signal, leaving bwrap already reparented to init -- which
    looks exactly like the flag not working and would make this test lie in the safe
    direction.
    """
    quoted = " ".join(shlex.quote(a) for a in argv)
    subprocess.run(
        ["/bin/sh", "-c", f"{quoted} >/dev/null 2>&1 & sleep {linger}"],
        cwd=str(workdir), check=True, timeout=60,
    )


def _await_start(marker: Path, *, limit: float = 30.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.2)
    raise AssertionError(
        f"the sandboxed command never started ({marker} was never written), so whatever "
        "this test goes on to observe about survival says nothing about --die-with-parent"
    )


@pytest.mark.integration
@needs_bwrap
def test_the_sandbox_dies_with_its_parent(tmp_path):
    """The flag has shipped since the module was written; only the argv was ever asserted.

    An argv assertion proves the flag was passed, not that anything acts on it. The claim
    that matters is that a sandboxed command cannot outlive the server that started it --
    otherwise a crashed or restarted server leaves shells running against the workspace
    with nothing left to reap them.
    """
    work = tmp_path / "work"
    work.mkdir()
    argv = sandbox.build_argv(cfg(), req(
        command="echo up > started; sleep 8; echo alive > survived",
        home=str(tmp_path / "home"), workdir=str(work)))
    sandbox.ensure_home(str(tmp_path / "home"))

    _orphaned_run(argv, work)
    _await_start(work / "started")
    time.sleep(10)

    assert not (work / "survived").exists(), (
        "the sandboxed command outlived the parent that started it"
    )


@pytest.mark.integration
@needs_bwrap
def test_without_die_with_parent_the_command_does_survive(tmp_path):
    """The half that proves the test above can fail.

    Same fixture, same orphaning, one flag removed. If this does not survive then the
    survival check is measuring the shell's exit or a timing accident rather than the flag,
    and the test above would pass against a sandbox that never reaped anything.
    """
    work = tmp_path / "work"
    work.mkdir()
    argv = sandbox.build_argv(cfg(), req(
        command="echo up > started; sleep 8; echo alive > survived",
        home=str(tmp_path / "home"), workdir=str(work)))
    argv.remove("--die-with-parent")
    sandbox.ensure_home(str(tmp_path / "home"))

    _orphaned_run(argv, work)
    _await_start(work / "started")
    time.sleep(10)

    assert (work / "survived").exists(), (
        "nothing survived even without --die-with-parent, so the positive test proves "
        "nothing about the flag"
    )


# --- secret denylist at the mount level ---------------------------------------------------


def globs_file(tmp_path: Path, *patterns: str) -> str:
    f = tmp_path / "globs.txt"
    f.write_text("# test denylist\n" + "\n".join(patterns) + "\n", encoding="utf-8")
    return str(f)


def test_a_shadowed_directory_becomes_a_tmpfs_and_a_file_gets_dev_null():
    """The two primitives are not interchangeable: a tmpfs needs a directory to mount on."""
    argv = sandbox.build_argv(cfg(), req(), shadows=(
        sandbox.ShadowTarget(path=f"{HOME}/.ssh", kind="dir", matched=".ssh/**"),
        sandbox.ShadowTarget(path=f"{HOME}/id_rsa", kind="file", matched="id_rsa*"),
    ))
    assert (f"{HOME}/.ssh",) in pairs(argv, "--tmpfs")
    assert ("/dev/null", f"{HOME}/id_rsa") in pairs(argv, "--ro-bind")


def test_shadows_come_after_every_bind():
    """Rule 3. A shadow covers a path inside a tree a bind created; emit it first and bwrap
    has nothing to mount on. Checked against the last bind, not the first, because the
    workdir bind is the one most likely to move."""
    work = "/mnt/c/Users/dev/proj"
    argv = sandbox.build_argv(
        cfg(), req(workdir=work, extra_binds=("/usr/local/bin",)),
        shadows=(sandbox.ShadowTarget(path=f"{work}/.env", kind="file", matched=".env"),))
    last_bind = max(
        i for i, a in enumerate(argv)
        if a in ("--bind", "--ro-bind") and argv[i + 2] in (HOME, work, "/usr/local/bin")
    )
    assert argv.index(f"{work}/.env") > last_bind


def test_no_shadows_leaves_the_argv_exactly_as_it_was():
    assert sandbox.build_argv(cfg(), req()) == sandbox.build_argv(cfg(), req(), shadows=())


@posix_only
def test_the_scan_finds_a_secret_file_and_a_secret_directory(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "known_hosts").write_text("x", encoding="utf-8")
    (home / "id_rsa").write_text("x", encoding="utf-8")
    (home / "notes.md").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".ssh/**", "id_rsa*")),
        req(home=str(home)))

    assert {t.path: t.kind for t in found} == {
        f"{home}/.ssh": "dir", f"{home}/id_rsa": "file"}


@posix_only
def test_a_matched_directory_is_not_descended_into(tmp_path):
    """Pruning keeps `.git/**` off every loose object, and stops a shadow being emitted
    inside another one, where the outer tmpfs hides the target the inner op needs."""
    home = tmp_path / "home"
    (home / ".ssh" / "nested").mkdir(parents=True)
    (home / ".ssh" / "nested" / "id_rsa").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".ssh/**", "id_rsa*")),
        req(home=str(home)))

    assert [t.path for t in found] == [f"{home}/.ssh"]


@posix_only
def test_the_workdir_is_scanned_too_not_only_home(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    (work / ".env").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".env")),
        req(home=str(home), workdir=str(work)))

    assert [t.path for t in found] == [f"{work}/.env"]


@posix_only
def test_extra_binds_are_scanned_now_that_an_agent_file_can_choose_them(tmp_path):
    """This asserts the opposite of what it used to, and the reversal is the point.

    It was `test_toolchain_binds_are_not_scanned`, and the exclusion was justified twice
    over: `extra_binds` were paths an operator chose, usually under /usr, so scanning them
    spent latency where credentials do not live; and shadowing inside a read-only bind
    protected nothing the bind did not already protect.

    M6 killed the first premise. An agent file supplies `extra_binds` now, and a markdown
    file anyone can add to a repository is not an operator decision -- the entire argument
    for trusting the value was that a person with server access had typed it. The second
    premise was never right for secrets: read-only means readable, and being read is the
    whole threat for a credential.

    So a bind holding a private key is covered up, exactly as a workdir holding one is.
    """
    home, tools = tmp_path / "home", tmp_path / "tools"
    home.mkdir()
    tools.mkdir()
    (tools / "id_rsa").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(home=str(home), extra_binds=(str(tools),)))

    assert [t.path for t in found] == [f"{tools}/id_rsa"]


@posix_only
def test_a_symlink_is_never_shadowed(tmp_path):
    """Measured, not assumed: a shadow op on a symlink node aborts the whole bwrap
    invocation rather than following or creating it. Fail-closed, so nothing leaks, but a
    ~/.ssh symlinked into a dotfiles repo would then kill every run_bash call."""
    home = tmp_path / "home"
    (home / "real").mkdir(parents=True)
    (home / "real" / "key").write_text("x", encoding="utf-8")
    (home / "id_rsa").symlink_to(home / "real" / "key")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(home=str(home)))

    assert found == ()


@posix_only
def test_the_scan_refuses_rather_than_cover_part_of_a_tree(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for i in range(12):
        (home / f"f{i}").write_text("x", encoding="utf-8")

    with pytest.raises(sandbox.SecretShadowIncomplete) as e:
        sandbox.discover_secret_shadows(
            cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*"),
                secret_shadow_max_entries=5),
            req(home=str(home)))
    assert "run_bash is refused" in str(e.value)


@posix_only
def test_a_tree_deeper_than_the_budget_refuses_too(tmp_path):
    home = tmp_path / "home"
    deep = home
    for i in range(6):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)

    with pytest.raises(sandbox.SecretShadowIncomplete):
        sandbox.discover_secret_shadows(
            cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*"),
                secret_shadow_max_depth=3),
            req(home=str(home)))


@pytest.mark.integration
@needs_bwrap
def test_a_denylisted_file_is_unreadable_from_inside_the_sandbox(tmp_path):
    """From inside, not from the argv. An argv assertion passes with the mount at the wrong
    path, which is the one mistake this feature can actually make."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "id_rsa").write_text("PRIVATE-KEY-BODY", encoding="utf-8")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(command="cat ~/id_rsa", home=str(home)))

    assert result.exit_code != 0
    assert "PRIVATE-KEY-BODY" not in result.stdout


@pytest.mark.integration
@needs_bwrap
def test_the_same_file_is_readable_when_the_denylist_does_not_match(tmp_path):
    """The half that makes the one above able to fail. Without it, an unreadable file is
    equally consistent with a broken bind, a wrong HOME, or a sandbox reading nothing."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "id_rsa").write_text("PRIVATE-KEY-BODY", encoding="utf-8")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, "nothing-matches-this")),
        req(command="cat ~/id_rsa", home=str(home)))

    assert result.exit_code == 0
    assert "PRIVATE-KEY-BODY" in result.stdout


@pytest.mark.integration
@needs_bwrap
def test_a_denylisted_directory_reads_empty_from_inside(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE-KEY-BODY", encoding="utf-8")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, ".ssh/**")),
        req(command="ls -A ~/.ssh; echo ---; cat ~/.ssh/id_ed25519 2>&1 || true",
            home=str(home)))

    listing = result.stdout.split("---")[0]
    assert listing.strip() == ""
    assert "PRIVATE-KEY-BODY" not in result.stdout


@pytest.mark.integration
@needs_bwrap
def test_a_symlinked_secret_does_not_break_the_sandbox(tmp_path):
    """The availability half of the symlink finding. A shadow op on a link aborts bwrap
    outright, so the walk skips links -- and this proves the command still runs."""
    home = tmp_path / "home"
    (home / "real").mkdir(parents=True)
    (home / "real" / "key").write_text("x", encoding="utf-8")
    (home / "id_rsa").symlink_to(home / "real" / "key")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(command="echo still-running", home=str(home)))

    assert result.exit_code == 0
    assert result.stdout.strip() == "still-running"


@pytest.mark.integration
@needs_bwrap
def test_a_failed_shadow_means_the_command_never_runs(tmp_path):
    """Setup completes before exec, so a shadow is never applied around a running command.

    The question this answers is whether "covered up" implies a window in which the secret
    is readable. It does not, and the proof is negative: a shadow op on a symlink aborts
    bubblewrap, and if the command were executing while mounts were still being applied, an
    `echo` would have printed before the abort. Nothing prints. bwrap builds the whole mount
    namespace, pivots, and only then execs.
    """
    home = tmp_path / "home"
    (home / "real").mkdir(parents=True)
    (home / "id_rsa").symlink_to(home / "real")
    argv = sandbox.build_argv(cfg(), req(command="echo COMMAND-RAN", home=str(home)),
        shadows=(sandbox.ShadowTarget(
            path=str(home / "id_rsa"), kind="dir", matched="id_rsa*"),))
    sandbox.ensure_home(str(home))

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)

    assert proc.returncode != 0
    assert "COMMAND-RAN" not in proc.stdout, (
        "the command executed despite a failed mount, so setup and execution overlap and a "
        "shadow could be applied around a running command"
    )


@pytest.mark.integration
@needs_bwrap
def test_a_shadowed_secret_is_never_readable_however_early_it_is_read(tmp_path):
    """The positive half, raced rather than argued.

    A single run proves the shadow is in place by the time a command gets around to reading.
    Reading as the very first instruction, repeatedly, is what would land in a startup window
    if one existed.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "id_rsa").write_text("PRIVATE-KEY-BODY", encoding="utf-8")
    config = cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*"))

    leaked = [
        r.stdout for _ in range(25)
        if "PRIVATE-KEY-BODY" in (r := sandbox.run(
            config, req(command="cat ~/id_rsa", home=str(home)))).stdout
    ]

    assert leaked == []


# ---- opaque directories (ADR-0041) ------------------------------------------------
def opaque_file(tmp_path: Path, *patterns: str) -> str:
    p = tmp_path / "opaque_globs.txt"
    p.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    return str(p)


@posix_only
def test_an_opaque_directory_is_covered_and_not_walked(tmp_path):
    """Both halves in one assertion, because either alone is the wrong feature.

    Covered but walked is merely slow. Walked but uncovered -- pruning without a mount --
    is the hole: a secret inside the directory would then be visible from a shell with
    nothing reporting that it was never looked at.
    """
    home = tmp_path / "home"
    (home / ".venv" / "deep").mkdir(parents=True)
    (home / ".venv" / "deep" / ".env").write_text("SECRET=1", encoding="utf-8")
    (home / "src.py").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".env"),
            opaque_globs_file=opaque_file(tmp_path, ".venv/**")),
        req(home=str(home)))

    assert [t.path for t in found] == [f"{home}/.venv"], (
        "the opaque directory itself must be the only shadow: a second op for the .env "
        "inside it would mean the walk descended, and no op at all would mean it was "
        "skipped without being covered")
    assert found[0].kind == "dir"
    assert found[0].matched.startswith("opaque:"), (
        "an opaque match must be attributable as one; reporting it as a denylist hit "
        "would make the security list look like it fired when it did not")


@posix_only
def test_an_opaque_directory_costs_no_budget(tmp_path):
    """The bug this feature exists for: a real project workdir refused outright.

    The tree here is larger than the budget and would refuse if walked, exactly as this
    repository's own checkout did once it carried a virtualenv.
    """
    home = tmp_path / "home"
    bulk = home / ".venv"
    bulk.mkdir(parents=True)
    for i in range(40):
        (bulk / f"f{i}").write_text("x", encoding="utf-8")
    (home / "src.py").write_text("x", encoding="utf-8")

    tight = cfg(secret_globs_file=globs_file(tmp_path, ".env"),
                opaque_globs_file=opaque_file(tmp_path, ".venv/**"),
                secret_shadow_max_entries=10)
    found = sandbox.discover_secret_shadows(tight, req(home=str(home)))
    assert [t.path for t in found] == [f"{home}/.venv"]

    # The control: the same tree, the same budget, without the opaque list. If this did
    # not refuse, the test above would prove nothing about the walk being skipped.
    with pytest.raises(sandbox.SecretShadowIncomplete):
        sandbox.discover_secret_shadows(
            cfg(secret_globs_file=globs_file(tmp_path, ".env"),
                opaque_globs_file=opaque_file(tmp_path, "matches-nothing/**"),
                secret_shadow_max_entries=10),
            req(home=str(home)))


@posix_only
def test_the_denylist_wins_over_the_opaque_list(tmp_path):
    """A directory on both lists is reported as the secret it is, not as bulk."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".ssh/**"),
            opaque_globs_file=opaque_file(tmp_path, ".ssh/**")),
        req(home=str(home)))

    assert [(t.path, t.matched) for t in found] == [(f"{home}/.ssh", ".ssh/**")]


@posix_only
def test_a_missing_opaque_list_is_not_fatal(tmp_path):
    """Unlike the denylist, whose absence must stop the server.

    An empty opaque list costs time. An empty denylist costs the whole control, and the
    two failures must not be handled the same way.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "id_rsa").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*"),
            opaque_globs_file=str(tmp_path / "does-not-exist.txt")),
        req(home=str(home)))
    assert [t.path for t in found] == [f"{home}/id_rsa"]

    with pytest.raises(PathPolicyError):
        sandbox.discover_secret_shadows(
            cfg(secret_globs_file=str(tmp_path / "also-missing.txt"),
                opaque_globs_file=opaque_file(tmp_path, ".venv/**")),
            req(home=str(home)))


@pytest.mark.integration
@needs_bwrap
def test_a_secret_inside_an_opaque_directory_is_unreadable_from_inside(tmp_path):
    """The claim that makes skipping safe, checked from inside the sandbox.

    The walk never sees this file. If covering the parent did not hide it, the shell would
    read it -- and no argv assertion would notice, because there is no op for this path.
    """
    home = tmp_path / "home"
    (home / ".venv").mkdir(parents=True)
    (home / ".venv" / ".env").write_text("PRIVATE-KEY-BODY", encoding="utf-8")

    result = sandbox.run(
        cfg(secret_globs_file=globs_file(tmp_path, "nothing-matches-this"),
            opaque_globs_file=opaque_file(tmp_path, ".venv/**")),
        req(command="cat ~/.venv/.env", home=str(home)))

    assert result.exit_code != 0
    assert "PRIVATE-KEY-BODY" not in result.stdout


# --- resource limits ----------------------------------------------------------------------
#
# bwrap 0.9.0 has no --rlimit flag at all -- measured, 2026-09-05 -- so the caps come from
# a `prlimit` launcher in front of it rather than from bwrap. A launcher rather than
# subprocess's preexec_fn because tool calls run through asyncio.to_thread, so this process
# is multi-threaded and preexec_fn is a documented deadlock risk there. Putting it in the
# argv is also what makes these assertable here, where there is no bwrap.


def limiter(argv: list[str]) -> list[str]:
    """The prlimit prefix, up to and including its `--` terminator. Empty if there is none."""
    return argv[:argv.index("--") + 1] if argv and argv[0].endswith("prlimit") else []


def test_the_limits_are_applied_before_bwrap_not_by_it():
    """bwrap has no --rlimit, so the caps have to come from something that runs first."""
    argv = sandbox.build_argv(cfg(), req())
    prefix = limiter(argv)
    assert prefix, "no prlimit prefix, so nothing is bounding the command"
    assert argv[len(prefix)].endswith("bwrap"), "bwrap must run under the limiter, not beside it"
    assert not [a for a in argv if a.startswith("--rlimit")], "bwrap 0.9.0 has no such flag"


def test_each_limit_reaches_the_launcher_and_scales_to_bytes():
    argv = sandbox.build_argv(cfg(
        sandbox_max_memory_mb=64, sandbox_max_file_mb=8, sandbox_max_processes=99), req())
    prefix = limiter(argv)
    assert f"--as={64 * 1024 * 1024}" in prefix
    assert f"--fsize={8 * 1024 * 1024}" in prefix
    assert "--nproc=99" in prefix


def test_core_dumps_are_pinned_off_rather_than_inherited():
    """RLIMIT_FSIZE kills with SIGXFSZ, a core-dumping signal, so a host whose soft limit is
    not already zero would write a core file per overrun.

    Asserted on the argv and deliberately nowhere else. This machine's inherited limit is
    already zero, so the flag changes nothing observable here -- the "(core dumped)" a shell
    prints alongside SIGXFSZ comes from the kernel setting WCOREDUMP, which it does anyway
    because core_pattern is a pipe, and no file is written either way. An integration test
    asserting "no core is left behind" was written first, passed with the flag removed, and
    was deleted: it could not fail, which is worse than not testing it.
    """
    assert "--core=0" in limiter(sandbox.build_argv(cfg(), req()))


def test_zero_removes_one_limit_without_removing_the_others():
    argv = sandbox.build_argv(cfg(sandbox_max_memory_mb=0), req())
    prefix = limiter(argv)
    assert not [a for a in prefix if a.startswith("--as=")]
    assert [a for a in prefix if a.startswith("--nproc=")], "the others must survive"


def test_zeroing_every_limit_removes_the_launcher_entirely():
    """The companion. Without it, a build that always emitted the prefix would pass the
    tests above, and an operator who turned everything off would still need prlimit
    installed to run anything at all."""
    argv = sandbox.build_argv(cfg(
        sandbox_max_memory_mb=0, sandbox_max_file_mb=0, sandbox_max_processes=0), req())
    assert not limiter(argv)
    assert argv[0].endswith("bwrap")


def test_the_tmpfs_is_sized_and_the_size_precedes_the_mount_it_applies_to():
    """`--size` applies to the *next* --tmpfs, so the pair is order-sensitive and a size
    emitted anywhere else silently sizes the wrong mount or nothing at all."""
    argv = sandbox.build_argv(cfg(sandbox_tmpfs_mb=16), req())
    i = index_of(argv, "--tmpfs", "/tmp")
    assert argv[i - 2] == "--size" and argv[i - 1] == str(16 * 1024 * 1024)


def test_zero_leaves_the_tmpfs_unsized():
    argv = sandbox.build_argv(cfg(sandbox_tmpfs_mb=0), req())
    i = index_of(argv, "--tmpfs", "/tmp")
    assert argv[i - 2] != "--size"


def test_a_secret_shadow_is_a_sized_tmpfs_too():
    """A shadow is a writable mount placed over every denylist match, so an unbounded one is
    a way to fill RAM. It exists to be empty, so it is sized small rather than given a knob."""
    argv = sandbox.build_argv(cfg(), req(), shadows=(
        sandbox.ShadowTarget(path=f"{HOME}/.ssh", kind="dir", matched=".ssh/**"),))
    i = index_of(argv, "--tmpfs", f"{HOME}/.ssh")
    assert argv[i - 2] == "--size"
    assert int(argv[i - 1]) == sandbox._SHADOW_TMPFS_BYTES


def test_a_missing_limiter_is_refused_rather_than_run_unbounded(monkeypatch):
    """ADR-0034's rule: a control that is silently off is worse than one that is absent."""
    monkeypatch.setattr(sandbox.shutil, "which",
                        lambda name: None if "prlimit" in name else "/usr/bin/bwrap")
    with pytest.raises(SandboxUnavailable, match="cannot be bounded"):
        sandbox.run(cfg(), req())


def test_a_missing_limiter_is_fine_when_nothing_is_being_limited(monkeypatch):
    """The companion, and the reason `limiter_available` asks about the config rather than
    only about the binary: an operator who set every limit to 0 said there are none, and
    should not then need the tool that applies them."""
    monkeypatch.setattr(sandbox.shutil, "which",
                        lambda name: None if "prlimit" in name else "/usr/bin/bwrap")
    assert sandbox.limiter_available(cfg(
        sandbox_max_memory_mb=0, sandbox_max_file_mb=0, sandbox_max_processes=0))
    assert not sandbox.limiter_available(cfg())


@pytest.mark.integration
@needs_bwrap
def test_a_runaway_allocation_is_refused_by_the_memory_cap(tmp_path):
    """The demand-side OOM this exists for. Bounded on purpose: 256MB cap, 400MB ask."""
    home = tmp_path / "home"
    home.mkdir()
    result = sandbox.run(
        cfg(sandbox_max_memory_mb=256),
        req(command="python3 -c 'b = bytearray(400 * 1024 * 1024)' 2>&1 | tail -1",
            home=str(home)))
    assert "MemoryError" in result.stdout + result.stderr


@pytest.mark.integration
@needs_bwrap
def test_an_ordinary_allocation_under_the_cap_still_succeeds(tmp_path):
    """The companion. Without it a cap of zero bytes -- or a launcher that refused
    everything -- would satisfy the test above while breaking every real command."""
    home = tmp_path / "home"
    home.mkdir()
    result = sandbox.run(
        cfg(sandbox_max_memory_mb=1024),
        req(command="python3 -c 'b = bytearray(64 * 1024 * 1024); print(\"allocated\")'",
            home=str(home)))
    assert "allocated" in result.stdout
    assert result.exit_code == 0


@pytest.mark.integration
@needs_bwrap
def test_the_tmpfs_size_stops_a_write_filling_ram(tmp_path):
    """A 2MB /tmp given an 8MB write. ENOSPC is an ordinary error the command can report,
    which is why sizing the mount is better than leaving it to the memory cap."""
    home = tmp_path / "home"
    home.mkdir()
    result = sandbox.run(
        cfg(sandbox_tmpfs_mb=2),
        req(command="dd if=/dev/zero of=/tmp/big bs=1M count=8 >/dev/null 2>&1; "
                    "echo exit=$?; wc -c < /tmp/big",
            home=str(home)))
    assert "exit=1" in result.stdout, result.stdout
    assert "2097152" in result.stdout, "the file should stop at exactly the tmpfs size"


@pytest.mark.integration
@needs_bwrap
def test_the_file_size_cap_truncates_the_write(tmp_path):
    """RLIMIT_FSIZE kills the writer with SIGXFSZ, leaving the file at exactly the cap.

    The tmpfs is sized well above the cap on purpose, so what stops the write is the file
    limit and not the mount running out of room -- otherwise this would pass with
    sandbox_max_file_mb doing nothing at all.
    """
    home = tmp_path / "home"
    home.mkdir()
    result = sandbox.run(
        cfg(sandbox_max_file_mb=1, sandbox_tmpfs_mb=64),
        req(command="dd if=/dev/zero of=/tmp/big bs=1M count=8 >/dev/null 2>&1; "
                    "echo exit=$?; wc -c < /tmp/big",
            home=str(home)))
    out = result.stdout + result.stderr
    assert "exit=153" in out, f"expected death by SIGXFSZ (128+25), got: {out}"
    assert "1048576" in out, "the file should stop at exactly the cap"
