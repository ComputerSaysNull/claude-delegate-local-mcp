"""Four scripts redirect the bytecode cache to a temp directory and never remove it.

CLAUDE.md requires it: a tool comparing a committed artefact against live source must not
read a cached compile, and `sys.pycache_prefix` pointed at a fresh temp directory is what
forces one. The freshness is right. The cleanup was missing.

Measured 2026-09-10 in WSL: `/tmp` held **2,268 `cdl-gen-pyc-*` directories totalling
3.5 GB**, the oldest dated 25 August. The gate runs on every commit, so every commit since
then leaked one, on a machine with 7.5 GB free on its system drive.

The test runs each script as a subprocess, because that is how they are used and because a
finalizer registered with `atexit` only runs when a real process exits -- importing the
module would test a path nothing takes.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TMP = Path(tempfile.gettempdir())

# Each script with the prefix it redirects the cache to, and an argument list that makes it
# do its work and exit without writing to the repository.
SCRIPTS = [
    ("scripts/docs_gate.py", "cdl-gate-pyc-", ["--owner", "scripts/docs_gate.py"]),
    ("scripts/gen_config_docs.py", "cdl-gen-pyc-", ["--check"]),
    ("scripts/gen_gitleaks_config.py", "cdl-gl-pyc-", ["--check"]),
    ("scripts/gen_tools_docs.py", "cdl-gentools-pyc-", ["--check"]),
]


def dirs_with(prefix: str) -> set[Path]:
    return {p for p in TMP.glob(f"{prefix}*") if p.is_dir()}


@pytest.mark.parametrize(("script", "prefix", "args"), SCRIPTS, ids=[s[0] for s in SCRIPTS])
def test_a_script_removes_the_cache_directory_it_created(
    script: str, prefix: str, args: list[str]
) -> None:
    before = dirs_with(prefix)

    result = subprocess.run(
        [sys.executable, script, *args], cwd=REPO_ROOT, capture_output=True, text=True
    )

    # The run has to have actually happened, or "no directory was left behind" is what a
    # script that died on its first line would also produce.
    assert result.returncode in (0, 1), (
        f"{script} did not run to a normal conclusion:\n{result.stdout}\n{result.stderr}"
    )

    leaked = dirs_with(prefix) - before
    assert not leaked, (
        f"{script} left {len(leaked)} cache director(y/ies) behind: {sorted(leaked)}. "
        "sys.pycache_prefix must still point at a fresh temp directory -- the fix is to "
        "remove it on exit, not to stop creating it."
    )


@pytest.mark.parametrize(("script", "prefix", "args"), SCRIPTS, ids=[s[0] for s in SCRIPTS])
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
