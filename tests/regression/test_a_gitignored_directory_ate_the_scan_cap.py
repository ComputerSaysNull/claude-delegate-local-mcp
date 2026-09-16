"""A gitignored directory ate the scan cap, so the search answered from nothing.

`_search_candidates` prunes two things in the walk -- symlinks, and anything the secret
denylist matches -- and never asks git. So the walk descends into whatever the project
ignores, charges `search_max_files_scanned` against files no search should ever see, and
returns having never reached the source.

Measured at this repository's root on 2026-09-16: 4,059 of 4,206 `.py` files (96.5%) and
1,543 of 1,564 directories (98.7%) are gitignored. A root-scoped search spent its whole
2,000-file cap inside them and reported 2 matching lines where 351 exist.

The fix prunes in the walk, so the cap is never charged for them. It is deliberately
gitignore and *not* the denylist: the denylist is a security control (ADR-0010) and the
list of bulk directories a project does not want read is the project's own statement,
which it already wrote down. The fixture below is named `installed` for exactly that
reason -- no shipped glob matches it, so only `.gitignore` can make these tests pass.

`resolve_permitted` still runs `gitignored` over the survivors, and must: an ignored file
inside a directory that is not ignored is not caught by pruning directories.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

# The walk resolves real paths and shells out to git; the server runs in WSL, so this is
# proven there rather than on the Windows run.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="LAYER 1 UNPROVEN BY THIS RUN -- run it under WSL, where the server lives.",
)

CAP = 10
BULK = 30


def cfg(root: Path, **over) -> Config:
    """The workspace root is the project's *parent*.

    A path that resolves to a workspace root is refused as a scope (#212), so a fixture
    whose project is itself the root cannot search at all -- it fails on the refusal
    rather than on the bug, which is a red that proves nothing.
    """
    kw = {
        "workspace_roots": (str(root),),
        "respect_gitignore": True,
        "search_max_files_scanned": CAP,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real repository, shaped the way the bug needs: bulk first, source second.

    `installed` sorts before `src`, and the walk sorts its directory names, so the cap is
    charged inside the ignored tree before the source is ever reached. That ordering is
    the bug, not an accident of the fixture.
    """
    work = tmp_path / "proj"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (work / ".gitignore").write_text("installed/\n", encoding="utf-8")

    bulk = work / "installed" / "lib"
    bulk.mkdir(parents=True)
    for i in range(BULK):
        (bulk / f"mod{i}.py").write_text("def alpha():\n    return 0\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    return work


def search(scope: Path, **over) -> str:
    result = tools.execute_tool(
        cfg(scope.parent, **over),
        call("search_files", path=str(scope), pattern="def alpha"),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, result.content
    return result.content


@posix_only
def test_the_source_is_found_behind_a_gitignored_directory(project):
    """The bug as a user meets it: a search that answers from nothing.

    `BULK` is three times `CAP`, so without pruning the walk is finished before it reaches
    `src` and the tool reports no match at all.
    """
    report = search(project)

    assert "app.py" in report, (
        "the search never reached src/: the scan cap was charged against the gitignored "
        "tree, which is the bug this test is named for")
    assert "mod0.py" not in report, "an ignored file reached the results"


@posix_only
def test_the_cap_is_not_reported_as_reached(project):
    """The second half of the symptom, and the half a user actually acts on.

    Reaching the cap makes the tool say the answer is not exhaustive and tell the caller
    to narrow the search -- advice that is wrong here, because narrowing was never the
    problem. A pruned walk is genuinely exhaustive and must not say otherwise.
    """
    report = search(project)

    assert "scan cap" not in report.lower(), (
        "the walk still charged the cap against ignored files, so the result claims to be "
        "non-exhaustive when it is complete")


@posix_only
def test_without_gitignore_the_same_tree_is_still_eaten(project):
    """The control.

    Both tests above would pass against a walk that got faster for some unrelated reason
    -- a cheaper policy, a different sort order, a denylist that happened to match. With
    `respect_gitignore` off, nothing may prune the bulk tree, so the cap must still be
    eaten and `src` must still be missed. If this ever passes, the two tests above have
    stopped proving that *gitignore* is what does the pruning.
    """
    report = search(project, respect_gitignore=False)

    assert "app.py" not in report, (
        "src was reached with gitignore disabled, so something other than gitignore is "
        "pruning the bulk tree and the tests above prove nothing")
