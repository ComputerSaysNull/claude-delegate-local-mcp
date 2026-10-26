"""`sandbox_max_processes` was spent by the rest of the host (PLAN M21.11).

The cap is applied as RLIMIT_NPROC, which the kernel counts over every thread the user
owns -- the desktop, the editor's remote server, the other Claude sessions (ADR-0054). On
a host whose total sits above the cap, every delegated `run_bash` fails before it starts,
with "Creating new namespace failed: Resource temporarily unavailable". The fix puts the
cap on a transient systemd user scope, whose TasksMax counts only the command's own
processes (ADR-0129). These two tests prove the two halves: a trivial command runs where
the old cap would have killed it, and the cap still binds a fork bomb.

Both are skipped when no user manager is reachable, because without one the cap falls back
to the old, host-wide RLIMIT_NPROC -- the very bug -- and there is nothing new to prove.
"""

import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from claude_delegate_local import config as config_module
from claude_delegate_local import sandbox

def _cfg(**over):
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return config_module.Config(**kw)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def _sandbox_unusable() -> str:
    """Empty when a real sandbox runs here, else why not. As the masked-failure test says:
    an installed bwrap that cannot unshare is the host, not the feature, being broken.

    Probed with no process cap: under the default cap, the very bug these tests catch makes
    the probe fail, and they would skip on the unfixed code instead of failing there."""
    if not Path("/bin/sh").exists():
        return "POSIX only: the sandbox never runs on Windows"
    cfg = _cfg(sandbox_max_processes=0)
    if not sandbox.available(cfg):
        return "bwrap is not installed"
    with tempfile.TemporaryDirectory() as home:
        try:
            probe = sandbox.run(cfg, sandbox.SandboxRequest(command="true", home=home))
        except Exception as e:  # any failure at all here means "cannot run one"
            return f"bwrap could not run: {e}"
    if probe.exit_code != 0:
        return (
            "THE END-TO-END SANDBOX TESTS DID NOT RUN -- bwrap cannot run a command here "
            f"(`true` exited {probe.exit_code}). The binds are unproven by this run."
        )
    return ""


needs_sandbox = pytest.mark.skipif(bool(_sandbox_unusable()), reason=_sandbox_unusable())


@lru_cache(maxsize=1)
def _no_scope() -> str:
    """Empty when a transient user scope is reachable here, else why not."""
    if not sandbox.scope_available(_cfg()):
        return (
            "No transient systemd user scope is reachable here (systemd-run --user found "
            "no bus), so the process cap falls back to RLIMIT_NPROC -- the old, host-wide "
            "cap this regression is about -- and the scope half is unproven by this run."
        )
    return ""


needs_scope = pytest.mark.skipif(bool(_no_scope()), reason=_no_scope())


# ---- the two halves ----------------------------------------------------------------


@pytest.mark.integration
@needs_sandbox
@needs_scope
def test_a_trivial_command_runs_under_the_process_cap(tmp_path):
    """On today's code this fails with the namespace error, because the cap is shared."""
    home = tmp_path / "home"
    result = sandbox.run(
        _cfg(sandbox_max_processes=64),
        sandbox.SandboxRequest(command="true", home=str(home)),
    )
    assert result.exit_code == 0, result.stderr


@pytest.mark.integration
@needs_sandbox
@needs_scope
def test_the_cap_still_binds_a_fork_bomb(tmp_path):
    """The negative control: without it, the test above passes on a cap that does nothing."""
    home = tmp_path / "home"
    command = (
        "python3 -I -c \""
        "import os, time\n"
        "refused = 0\n"
        "kids = []\n"
        "for _ in range(100):\n"
        "    try:\n"
        "        p = os.fork()\n"
        "    except OSError:\n"
        "        refused += 1\n"
        "    else:\n"
        "        if p == 0:\n"
        "            time.sleep(3)\n"
        "            os._exit(0)\n"
        "        kids.append(p)\n"
        "for p in kids:\n"
        "    os.waitpid(p, 0)\n"
        "print('refused', refused)\n"
        "\""
    )
    result = sandbox.run(
        _cfg(sandbox_max_processes=64),
        sandbox.SandboxRequest(command=command, home=str(home)),
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "refused" in result.stdout, result.stdout + result.stderr
    refused = int(result.stdout.split("refused", 1)[1].strip())
    assert refused > 0


@pytest.mark.integration
@needs_sandbox
@needs_scope
def test_the_scope_leaves_a_commands_variables_to_its_shell(tmp_path, monkeypatch):
    """systemd-run expands `${VAR}` in its argv by default, from the server's environment.
    A variable `--clearenv` withholds then reached the command, and the ERR trap's own
    expansions were emptied, with a warning on every command's stderr."""
    monkeypatch.setenv("PROBE_HOST_ONLY_VALUE", "from-the-host")
    result = sandbox.run(
        _cfg(sandbox_max_processes=64),
        sandbox.SandboxRequest(
            command='echo "[${PROBE_HOST_ONLY_VALUE}]"', home=str(tmp_path / "home")),
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
    assert "Invalid environment variable" not in result.stderr, result.stderr


@pytest.mark.integration
@needs_sandbox
@needs_scope
def test_the_suites_runtime_dir_still_reaches_the_user_manager():
    """conftest points XDG_RUNTIME_DIR at a temporary folder. Without the user manager's
    sockets linked into it, a fresh probe found no bus and every sandbox test in the suite
    fell back to the host-wide cap: at 579 user threads, 26 of 47 failed."""
    sandbox._scope_available_bin.cache_clear()  # the skip probe ran before the fixture
    assert sandbox.scope_available(_cfg())
