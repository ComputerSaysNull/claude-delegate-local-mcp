"""Four scripts redirect the bytecode cache to a temp directory and never removed it.

CLAUDE.md requires the redirect: a tool comparing a committed artefact against live source
must not read a cached compile, and `sys.pycache_prefix` pointed at a fresh temp directory
is what forces one. The freshness was right. The cleanup was missing.

Measured 2026-09-10 in WSL: `/tmp` held **34,405 `cdl-*-pyc-*` directories totalling
4.6 GB**, the oldest dated 25 August, on a machine with 7.5 GB free on its system drive.
`docs_gate.py` runs on every commit and on every gate invocation, so it produced most of
them.

**Each subprocess gets its own temp directory**, rather than the test watching the shared
one. The first version of this test snapshotted the system temp directory for
`cdl-*-pyc-*` before and after its own subprocess, which is a namespace every other test
writes to as well: under `pytest-xdist` the 56 gate self-check tests each spawn
`docs_gate.py`, so another worker's in-flight directory landed in this test's "after" set
and was attributed to a subprocess that had already cleaned up after itself. It passed on
one CI matrix entry and failed on the other, from the same commit. Isolating the child's
`TMPDIR` removes the shared namespace instead of trying to time around it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Each script with the prefix it redirects the cache to, and an argument list that makes it
# do its work and exit without writing to the repository.
SCRIPTS = [
    ("scripts/docs_gate.py", "cdl-gate-pyc-", ["--owner", "scripts/docs_gate.py"]),
    ("scripts/gen_config_docs.py", "cdl-gen-pyc-", ["--check"]),
    ("scripts/gen_gitleaks_config.py", "cdl-gl-pyc-", ["--check"]),
    ("scripts/gen_tools_docs.py", "cdl-gentools-pyc-", ["--check"]),
]
IDS = [s[0] for s in SCRIPTS]


def run_isolated(script: str, args: list[str], private_tmp: Path) -> subprocess.CompletedProcess:
    """Run one script with `tempfile` pointed at a directory only this test can see.

    All three names are set because `tempfile.gettempdir()` consults TMPDIR, TEMP and TMP,
    and which one wins differs between POSIX and Windows -- setting one would silently
    leave the child on the shared directory on the other platform, which is the bug this
    function exists to remove.
    """
    env = {
        **os.environ,
        "TMPDIR": str(private_tmp),
        "TEMP": str(private_tmp),
        "TMP": str(private_tmp),
    }
    return subprocess.run(
        [sys.executable, script, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(("script", "prefix", "args"), SCRIPTS, ids=IDS)
def test_a_script_removes_the_cache_directory_it_created(
    script: str, prefix: str, args: list[str], tmp_path: Path
) -> None:
    private_tmp = tmp_path / "private"
    private_tmp.mkdir()

    result = run_isolated(script, args, private_tmp)

    # The run has to have actually happened, or "no directory was left behind" is what a
    # script that died on its first line would also produce.
    assert result.returncode in (0, 1), (
        f"{script} did not run to a normal conclusion:\n{result.stdout}\n{result.stderr}"
    )

    leaked = [p for p in private_tmp.glob(f"{prefix}*") if p.is_dir()]
    assert not leaked, (
        f"{script} left {len(leaked)} cache director(y/ies) behind: {leaked}. "
        "sys.pycache_prefix must still point at a fresh temp directory -- the fix is to "
        "remove it on exit, not to stop creating it."
    )


@pytest.mark.parametrize(("script", "prefix", "args"), SCRIPTS, ids=IDS)
def test_the_script_really_used_the_private_temp_directory(
    script: str, prefix: str, args: list[str], tmp_path: Path
) -> None:
    """The control on the isolation itself.

    If the child ignored the environment and kept using the shared temp directory, the
    test above would find an empty private directory and pass without ever observing the
    script's cleanup -- a check that cannot fail. This proves the redirect lands where the
    other test looks, by running the script with cleanup suppressed and requiring the
    directory to appear there.
    """
    private_tmp = tmp_path / "private"
    private_tmp.mkdir()

    # `-X` cannot disable atexit, so the suppression is done the way the runtime offers:
    # os._exit skips atexit handlers entirely. The script is imported and run under a
    # wrapper rather than edited, so the committed source is what is under test.
    wrapper = tmp_path / "no_atexit.py"
    wrapper.write_text(
        "import os, runpy, sys\n"
        "sys.argv = [sys.argv[1], *sys.argv[2:]]\n"
        "try:\n"
        "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "TMPDIR": str(private_tmp),
        "TEMP": str(private_tmp),
        "TMP": str(private_tmp),
    }
    subprocess.run(
        [sys.executable, str(wrapper), script, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )

    created = [p for p in private_tmp.glob(f"{prefix}*") if p.is_dir()]
    assert created, (
        f"{script} created no {prefix}* directory in the private temp directory, so the "
        "cleanup test above is watching somewhere the script never writes and could not "
        "fail. Check that TMPDIR/TEMP/TMP reach the child."
    )


@pytest.mark.parametrize(("script", "prefix", "args"), SCRIPTS, ids=IDS)
def test_the_cache_is_still_redirected_away_from_the_source_tree(
    script: str, prefix: str, args: list[str]
) -> None:
    """The negative control on the fix, not on the bug.

    Cleaning up by simply deleting the `pycache_prefix` line would make every assertion
    above pass while reintroducing the stale-compile bug the line exists to prevent. This
    asserts the redirect is still there, so the two cannot be traded against each other.
    """
    source = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert "sys.pycache_prefix" in source, (
        f"{script} no longer redirects the bytecode cache. Removing the leak must not "
        "remove the freshness guarantee -- see CLAUDE.md and JOURNAL 2026-08-25."
    )
    assert f'prefix="{prefix}"' in source
