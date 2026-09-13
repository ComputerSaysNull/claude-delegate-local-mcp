"""A delegation spent 40 minutes of a 44-minute run inside six unscoped searches.

`search_files` walks every workspace root when `path` is omitted, applies the path policy
to every candidate, then opens and regexes each permitted file -- all in the server
process, in series, over `/mnt/c`, which is roughly 12x slower per syscall (ADR-0020).
Measured 2026-09-13, tool time as `ms` minus `backend_ms`:

    no `path`, all roots        490s, 505s, 572s
    `path` = the whole repo     121s
    `path` = a subdirectory     0.7s, 2.4s, 6.1s
    `read_file`                 0.12s - 1.1s

So scope is worth about 100x and `read_file` was never the problem. The delegation was not
being careless: **the contract told it to do this**, in two of the four homes ADR-0066
assigns.

`glob`'s own description claimed "a test helper is found far *faster* with glob=test_*.py
than by reading directories". That is false -- `glob` narrows which files are *opened*,
never which are walked or policy-checked -- and the three worst calls in the transcript all
set `glob` and omitted `path`.

The refusal was worse, because it fires exactly when the model is already unsure. A guessed
`path` of `/workspace/...` was refused with the remedy "Name an existing directory or file,
or omit it to search everywhere." The delegation omitted it, and the next turn cost 239s.

Both halves are asserted here through behaviour rather than through wording, and no test
reads a tool description back out of `list_tools()` -- CLAUDE.md records that as one of the
six checks that could not fail, because it reads the server's own copy rather than the wire.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

# Resolving a real path needs a POSIX filesystem and the server runs in WSL, so the cases
# that actually walk a tree are proven there rather than here.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="LAYER 1 UNPROVEN BY THIS RUN -- run it under WSL, where the server lives.",
)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


@pytest.fixture
def haystack(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("alpha is documented here\n", encoding="utf-8")
    return tmp_path


def _search(root: Path, **args) -> str:
    result = tools.execute_tool(cfg(root), call("search_files", **args), tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    return result.content


@posix_only
def test_an_unscoped_search_says_what_it_scanned(haystack):
    """The half with teeth. The model reads results, so the cost is taught there.

    This originally argued for a note *rather than* a refusal, on the grounds that a `glob`
    with no `path` is a legitimate search -- finding every `conftest.py` anywhere is exactly
    that -- and that refusing would break a real use to fix an expensive one. That reasoning
    was right about the use and wrong about the remedy: the measurement after it shipped
    found the next delegation still opening unscoped, because a note cannot tell a model a
    directory name it has never been given. `path` is required now and `_unscoped_` keeps
    the legitimate use reachable, so the note survives and attaches to the sentinel.

    What this protects is unchanged: an all-roots search says what it scanned.
    """
    out = _search(haystack, pattern=r"alpha", path=tools.UNSCOPED)
    assert str(haystack) in out, out
    assert "path" in out


@posix_only
def test_a_scoped_search_is_not_nagged(haystack):
    """The control. A note on every result is noise, and noise is ignored."""
    out = _search(haystack, pattern=r"alpha", path=str(haystack / "pkg"))
    assert str(haystack / "pkg") not in out.split("\n")[-1]


@posix_only
def test_a_refused_path_names_the_roots_instead_of_recommending_the_slow_way(haystack):
    """The chain that turned a 4-second mistake into a 239-second one.

    The remedy told the model to omit `path`, which is the most expensive thing it can do,
    at the moment it had just demonstrated it did not know where anything was. It must name
    the roots it *would* accept instead.
    """
    result = tools.execute_tool(
        cfg(haystack),
        call("search_files", pattern="alpha", path="/workspace/src"),
        tools.ALL_TOOL_NAMES,
    )

    assert result.is_error, result.content
    assert str(haystack) in result.content, result.content
    assert "omit it to search everywhere" not in result.content, result.content
