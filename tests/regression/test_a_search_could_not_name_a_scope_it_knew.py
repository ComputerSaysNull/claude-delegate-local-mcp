"""Three homes told the model to scope a search, and none of them said what the scopes were.

ADR-0074 changed what the contract *claimed* about `path` in three of ADR-0066's four homes
-- the `inputSchema`, the result note, and the refusal remedy in `paths.py`. The next
session's first delegation opened with four unscoped `search_files` calls anyway.

Measured 2026-09-13, tool time as `ms` minus `backend_ms`, one delegation, same four
patterns each turn: `path` omitted cost 657.6s, `path` set to the repository root cost
391.8s, and `path` set to subdirectories cost 4.4s. ADR-0074's own before-figures were 490s,
505s and 572s, so the session after the fix was worse than anything the fix had measured.

The model was climbing toward scope by trial: told to narrow, it narrowed to the only place
it had ever been told the name of. A fourth wording could not have worked, because all three
homes were claims about an argument and what was missing was a map.

So `path` becomes required, with `_unscoped_` as an explicit and greppable escape, and the
declared description carries the workspace layout. **Directories and files both** -- a
directory holding only files would otherwise advertise as empty and be ruled out, and this
repository's root is exactly that shape.

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
    """A root whose top level holds a directory *and* a file, which is the shape at issue."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "deep.md").write_text("needle in a subdirectory\n", encoding="utf-8")
    (tmp_path / "top.md").write_text("needle at the top level\n", encoding="utf-8")
    return tmp_path


@posix_only
def test_a_search_without_a_path_is_refused(tree):
    """The bug. Today an omitted `path` walks every root and returns results."""
    with pytest.raises(ToolRefused) as caught:
        tools._search_files(cfg(tree), {"pattern": "needle"})

    assert "path" in str(caught.value)


@posix_only
def test_the_refusal_hands_over_the_layout_it_was_missing(tree):
    """A refusal that repeats "name a directory" is the thing that already failed.

    It fires exactly when the model has shown it does not know the layout, so it is the
    worst moment to be told only the root names and the best one to be handed the map.
    """
    with pytest.raises(ToolRefused) as caught:
        tools._search_files(cfg(tree), {"pattern": "needle"})

    message = str(caught.value)
    assert "pkg/" in message, "a real subdirectory name, marked as a directory"
    assert "top.md" in message, "a top-level FILE, or a file-only directory reads as empty"
    assert UNSCOPED in message, "and the escape, or the capability is unreachable"


@posix_only
def test_the_sentinel_still_walks_every_root(tree):
    """Control. The escape must actually escape, or it is decoration.

    Written against the bug the obvious implementation has: refusing everything that is not
    an absolute path would refuse the sentinel too, since layer 1 rejects it.
    """
    out = tools._search_files(cfg(tree), {"pattern": "needle", "path": UNSCOPED})

    assert "top.md" in out
    assert "deep.md" in out


@posix_only
def test_a_scoped_search_is_unaffected(tree):
    """Control. Must pass before the fix as well as after."""
    out = tools._search_files(cfg(tree), {"pattern": "needle", "path": str(tree / "pkg")})

    assert "deep.md" in out
    assert "top.md" not in out


@posix_only
def test_a_bad_regex_is_reported_before_the_missing_path(tree):
    """Control on ordering, and it must pass before the fix too.

    The path refusal now carries a whole workspace map. Burying a one-character regex fix
    underneath it would spend a turn sending the model to the wrong argument.
    """
    with pytest.raises(ToolRefused) as caught:
        tools._search_files(cfg(tree), {"pattern": "unclosed("})

    assert "regular expression" in str(caught.value)


@posix_only
def test_the_layout_names_files_and_directories_distinguishably(tree):
    """Directories carry a trailing slash; files do not.

    Without the marker the model cannot tell which entries it may scope *into*, and the
    whole point of listing files is that it can tell a file-only directory from an empty one.
    """
    layout = tools._workspace_layout(cfg(tree))

    assert "pkg/" in layout
    assert "top.md" in layout
    assert "top.md/" not in layout


@posix_only
def test_a_dense_directory_is_capped_and_says_so(tmp_path):
    """The listing sits in a cached prefix, so it cannot grow without bound.

    The count is what is kept rather than the names it replaces: "+N more" tells the model
    the directory is dense and worth scoping into, where silent truncation would suggest it
    had been shown everything.
    """
    for i in range(tools.LAYOUT_MAX_ENTRIES + 12):
        (tmp_path / f"file{i:03d}.md").write_text("x", encoding="utf-8")

    layout = tools._workspace_layout(cfg(tmp_path))

    assert "+12 more" in layout
    assert "file000.md" in layout
    assert f"file{tools.LAYOUT_MAX_ENTRIES + 11:03d}.md" not in layout
