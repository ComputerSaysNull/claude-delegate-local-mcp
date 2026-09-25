"""Regression: the workdir's tool caches were read-only inside the sandbox.

`.pytest_cache`, `.ruff_cache` and `__pycache__` under a bound workdir sit under opaque
mounts -- an empty read-only tmpfs (see `sandbox._shadow_argv`) -- so `ruff check` exited 2
until run with `--no-cache`, and pytest warned on every run. The fix points the caches at
sandbox scratch through the environment rather than by unmounting them: the opaque mounts
stay, keeping the host's caches out of the sandbox and stopping a command writing into them.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import sandbox, tools
from claude_delegate_local.config import Config

BWRAP_REASON = (
    "EXIT-CODE CAPTURE UNPROVEN BY THIS RUN -- this is not a pass. Capturing a real "
    "exit code needs a real bubblewrap, which exists in WSL and not on Windows. Run: "
    "wsl -d Ubuntu-24.04 -e bash -lc 'cd <repo> && ~/.venvs/delegate/bin/python -m "
    "pytest tests/regression/test_sandbox_tool_caches_were_read_only.py'"
)


def needs_bwrap(fn):
    """Both markers, always, because either alone is not enough. See tests/test_tools.py."""
    return pytest.mark.integration(
        pytest.mark.skipif(shutil.which("bwrap") is None, reason=BWRAP_REASON)(fn))


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


# --- the env redirection, pure -------------------------------------------------------------


def test_the_caches_are_pointed_at_sandbox_scratch_when_pytest_addopts_is_absent():
    """With nothing to preserve, the pytest cache option is set alone and the other two
    variables land under /tmp -- where the sandbox can write."""
    env = sandbox.scratch_cache_env({"LANG": "C.UTF-8"})
    assert env["LANG"] == "C.UTF-8"
    assert env["RUFF_CACHE_DIR"] == sandbox.SCRATCH_RUFF_CACHE_DIR
    assert env["PYTHONPYCACHEPREFIX"] == sandbox.SCRATCH_PYCACHE_PREFIX
    assert env["PYTEST_ADDOPTS"] == f"-o cache_dir={sandbox.SCRATCH_PYTEST_CACHE_DIR}"
    for value in env.values():
        if value.startswith("/"):
            assert value.startswith("/tmp/"), value


def test_the_deselect_list_survives_and_the_cache_dir_option_is_appended():
    """`provision.sandbox_env` may already carry a `PYTEST_ADDOPTS` deselect list; it must
    never be overwritten, only appended to."""
    env = sandbox.scratch_cache_env({"PYTEST_ADDOPTS": "--deselect a::b"})
    assert env["PYTEST_ADDOPTS"] == (
        "--deselect a::b -o cache_dir=/tmp/.cache/pytest"
    )


def test_an_existing_ruff_cache_dir_is_kept():
    """A command that already points ruff somewhere of its own is not overridden."""
    env = sandbox.scratch_cache_env({"RUFF_CACHE_DIR": "/host/cache"})
    assert env["RUFF_CACHE_DIR"] == "/host/cache"
    assert env["PYTHONPYCACHEPREFIX"] == sandbox.SCRATCH_PYCACHE_PREFIX
    assert env["PYTEST_ADDOPTS"] == f"-o cache_dir={sandbox.SCRATCH_PYTEST_CACHE_DIR}"


def test_an_existing_pycache_prefix_is_kept():
    env = sandbox.scratch_cache_env({"PYTHONPYCACHEPREFIX": "/host/pycache"})
    assert env["PYTHONPYCACHEPREFIX"] == "/host/pycache"
    assert env["RUFF_CACHE_DIR"] == sandbox.SCRATCH_RUFF_CACHE_DIR


# --- the real-run half ---------------------------------------------------------------------


@needs_bwrap
def test_a_command_still_runs_when_the_workdir_carries_an_opaque_cache_dir(tmp_path):
    """Through the same env path `_run_bash` uses, in a workdir carrying a covered
    `.pytest_cache`: the redirection must actually reach the sandbox, so a command can write
    the ruff cache under /tmp rather than into the read-only cover."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    (workdir / ".pytest_cache").mkdir()

    result = tools._run_bash(
        cfg(workdir),
        {"command": 'mkdir -p "$(dirname "$RUFF_CACHE_DIR")" && '
                    'touch "$RUFF_CACHE_DIR.probe" && echo "$RUFF_CACHE_DIR"'},
        tools.BashPolicy(workdir=str(workdir)),
    )

    assert result.outcome.exit_code == 0, result.text
    assert sandbox.SCRATCH_RUFF_CACHE_DIR in result.text
    assert sandbox.SCRATCH_RUFF_CACHE_DIR.startswith("/tmp/")
