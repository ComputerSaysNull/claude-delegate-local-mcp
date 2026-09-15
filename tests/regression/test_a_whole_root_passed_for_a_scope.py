"""`path` became required, and the model satisfied it with the whole repository root.

ADR-0076 made `path` required with `_unscoped_` as the escape, and gave the declared
description this deployment's workspace map. The mechanism works -- `path` is supplied on
every call. What it did not buy is scope.

Measured 2026-09-15 over one session's 13 dispatches, from the transcripts: **8 of 9** first
searches named a workspace root or the sentinel, and only **1** named a subdirectory. Across
every call, 15 of 44 still walked an entire root. `JOURNAL.md` 2026-09-13 priced that in a
controlled run -- same patterns, same delegation: a root cost **391.8s** where four named
subdirectories cost **4.4s**, so naming the root recovers almost none of the ~100x and sits
within 1.7x of the sentinel it is supposed to be an alternative to.

The declared text does not literally offer the root -- it says "a name ending in `/`", and
the roots are printed without one. That is what makes this the fourth failed wording rather
than a typo: three homes have now claimed something about `path` and the model has satisfied
each claim in the cheapest way that still parses. So the root stops being *accepted* rather
than merely being un-recommended, and the deliberate whole-root search keeps a spelling.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.config import Config
from claude_delegate_local.tools import UNSCOPED, ToolRefused

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="resolving a real path needs a POSIX filesystem; the server runs in WSL",
)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A root with a subdirectory and a top-level file, which is the shape at issue."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "deep.md").write_text("needle in a subdirectory\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("needle in the docs\n", encoding="utf-8")
    (tmp_path / "top.md").write_text("needle at the top level\n", encoding="utf-8")
    return tmp_path


@posix_only
def test_a_bare_workspace_root_is_refused(tree):
    """The bug. Today this walks the whole root and returns results."""
    with pytest.raises(ToolRefused) as caught:
        tools._search_files(cfg(tree), {"pattern": "needle", "path": str(tree)})

    assert "needle" not in str(caught.value), "a refusal, not a result"


@posix_only
def test_the_refusal_names_that_roots_own_children(tree):
    """The remedy travels with the refusal, or it is the fourth wording that failed.

    It must name the children of the root that was *just* refused, not the roots again: the
    call already proved the model knows the root name and not what is under it.
    """
    with pytest.raises(ToolRefused) as caught:
        tools._search_files(cfg(tree), {"pattern": "needle", "path": str(tree)})

    message = str(caught.value)
    assert "pkg/" in message, "a real subdirectory name, marked as a directory"
    assert "docs/" in message
    assert "top.md" in message, "and the files, or a file-only directory reads as empty"
    assert UNSCOPED in message, "and the escape, or the capability becomes unreachable"


@posix_only
def test_a_trailing_slash_does_not_smuggle_the_root_through(tree):
    """The obvious way past the check, and the one a string compare would miss."""
    with pytest.raises(ToolRefused):
        tools._search_files(cfg(tree), {"pattern": "needle", "path": str(tree) + "/"})


@posix_only
def test_a_subdirectory_is_still_accepted(tree):
    """Control. Must pass before the fix as well as after."""
    out = tools._search_files(cfg(tree), {"pattern": "needle", "path": str(tree / "pkg")})

    assert "deep.md" in out
    assert "top.md" not in out


@posix_only
def test_a_file_at_the_top_level_is_still_accepted(tree):
    """Control. Pointing at a file is the narrowest scope there is, not the widest.

    A check written against the root's *parenthood* rather than against its identity would
    refuse this too, and it is the one shape the layout explicitly advertises.
    """
    out = tools._search_files(cfg(tree), {"pattern": "needle", "path": str(tree / "top.md")})

    assert "top.md" in out


@posix_only
def test_the_sentinel_still_walks_every_root(tree):
    """Control. The deliberate whole-everything search must survive.

    This is the capability the refusal above would otherwise take away, so if this breaks
    the fix has removed a real shape rather than redirecting a wasteful one.
    """
    out = tools._search_files(cfg(tree), {"pattern": "needle", "path": UNSCOPED})

    assert "top.md" in out
    assert "deep.md" in out


@posix_only
def test_a_missing_path_is_still_refused_for_being_missing(tree):
    """Control on ordering. The two refusals are different facts and must stay distinct."""
    with pytest.raises(ToolRefused) as caught:
        tools._search_files(cfg(tree), {"pattern": "needle"})

    assert "required" in str(caught.value)
