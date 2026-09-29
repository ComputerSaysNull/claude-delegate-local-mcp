"""A file already prefetched into the prompt was read again, and the lines re-sent.

`files[]` put the file's lines in the prompt, yet a `read_file` of those lines ran the tool
and returned the same bytes again -- measured, 208 such re-reads and 4.2 MB of results for
lines the model already held. The fix seeds the read cache from the prefetch, so a read
whose lines the prefetch already holds answers from it: the first such read gets a pointer
rather than the lines, and a later one gets the lines themselves, prefixed to say where they
came from. Nothing runs in either case.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from claude_delegate_local import context, loop, tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.paths import ResolvedPath

from test_agentic_loop import ScriptedTurns, cfg, results_in, run, says, wants

# Every case that reads a real file needs a POSIX filesystem: on Windows the path policy
# refuses anything under `tmp_path`, so each read fails and a "served from cache" case passes
# for the wrong reason.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Reading a real file "
    "needs a POSIX filesystem; run this file in WSL.",
)


def _file(tmp_path, n, name="n.py"):
    p = tmp_path / name
    p.write_text("".join(f"line {i}\n" for i in range(1, n + 1)), encoding="utf-8")
    return str(p)


def _conf(tmp_path, **over):
    kw = {"workspace_roots": (str(tmp_path),)}
    kw.update(over)
    return cfg(**kw)


def _entry(real, text, nbytes, *, start_line=None, total_lines=None):
    return context.FileEntry(
        path=real, given=real, text=text, nbytes=nbytes, est_tokens=10,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        start_line=start_line, total_lines=total_lines,
    )


def _all_results(backend):
    """Every tool result the loop sent back, in the order it sent them."""
    return [r for req in backend.requests for r in results_in(req)]


def _whole_prefetch(path, n):
    """A whole-file prefetch, whose path is the resolved one the model will read."""
    real = os.path.realpath(path)
    text = "".join(f"line {i}\n" for i in range(1, n + 1))
    return (_entry(real, text, len(text.encode("utf-8"))),), real


def _range_prefetch(path, n, first, last):
    """A ranged prefetch of lines `first`..`last` of an `n`-line file."""
    real = os.path.realpath(path)
    all_lines = "".join(f"line {i}\n" for i in range(1, n + 1)).splitlines()
    text = "\n".join(all_lines[first - 1:last])
    return (
        _entry(real, text, len(text.encode("utf-8")),
               start_line=first, total_lines=n),
    ), real


# --- the pointer -------------------------------------------------------------------------


@posix_only
def test_a_whole_file_prefetched_is_not_read_again(tmp_path):
    """The bug. The file is already in the files block, so the read must not run."""
    path = _file(tmp_path, 10)
    prefetched, real = _whole_prefetch(path, 10)
    backend = ScriptedTurns(
        wants(("read_file", {"path": real})),
        says("done"),
    )
    result = run(
        backend, allowed=frozenset({"read_file"}),
        cfg=_conf(tmp_path), prefetched=prefetched,
    )

    assert result.deduped == 1
    served = _all_results(backend)[0]
    assert served.content == loop.PREFETCHED_POINTER.format(path=real)
    assert result.tool_errors == 0


@posix_only
def test_a_range_of_prefetched_lines_is_answered_with_a_pointer(tmp_path):
    """A read whose lines a ranged prefetch already holds is also a pointer."""
    path = _file(tmp_path, 10)
    prefetched, real = _range_prefetch(path, 10, 3, 6)
    backend = ScriptedTurns(
        wants(("read_file", {"path": real, "start_line": 4, "end_line": 5})),
        says("done"),
    )
    result = run(
        backend, allowed=frozenset({"read_file"}),
        cfg=_conf(tmp_path), prefetched=prefetched,
    )

    assert result.deduped == 1
    served = _all_results(backend)[0]
    assert served.content == loop.PREFETCHED_POINTER.format(path=real)


# --- what does not cover -----------------------------------------------------------------


@posix_only
def test_a_read_sticking_out_past_the_prefetched_range_runs(tmp_path):
    """The other direction: coverage that matched too widely answers the wrong question."""
    path = _file(tmp_path, 10)
    prefetched, real = _range_prefetch(path, 10, 3, 6)
    backend = ScriptedTurns(
        wants(("read_file", {"path": real, "start_line": 5, "end_line": 9})),
        says("done"),
    )
    result = run(
        backend, allowed=frozenset({"read_file"}),
        cfg=_conf(tmp_path), prefetched=prefetched,
    )

    assert result.deduped == 0
    served = _all_results(backend)[0]
    assert not served.content.startswith(loop.PREFETCHED_POINTER)
    assert " 5\tline 5" in served.content


@posix_only
def test_a_write_before_the_read_runs_it(tmp_path):
    """A write clears the cache, seeded entries included: the read is then fresh."""
    path = _file(tmp_path, 10)
    out = str(tmp_path / "out.txt")
    prefetched, real = _whole_prefetch(path, 10)
    backend = ScriptedTurns(
        wants(("write_file", {"path": out, "content": "x"})),
        wants(("read_file", {"path": real})),
        says("done"),
    )
    result = run(
        backend, allowed=frozenset({"read_file", "write_file"}),
        cfg=_conf(tmp_path), prefetched=prefetched,
    )

    assert result.deduped == 0
    served = _all_results(backend)[1]
    assert not served.content.startswith(loop.PREFETCHED_POINTER)
    assert " 1\tline 1" in served.content


@posix_only
def test_a_read_of_a_file_that_was_not_prefetched_runs(tmp_path):
    """No prefetched entry for this path means the cache has nothing to answer from."""
    path = _file(tmp_path, 10)
    other = _file(tmp_path, 10, name="other.py")
    prefetched, _ = _whole_prefetch(path, 10)
    other_real = os.path.realpath(other)
    backend = ScriptedTurns(
        wants(("read_file", {"path": other_real})),
        says("done"),
    )
    result = run(
        backend, allowed=frozenset({"read_file"}),
        cfg=_conf(tmp_path), prefetched=prefetched,
    )

    assert result.deduped == 0
    served = _all_results(backend)[0]
    assert not served.content.startswith(loop.PREFETCHED_POINTER)
    assert " 1\tline 1" in served.content


# --- the pointer once, then the lines ----------------------------------------------------


@posix_only
def test_a_second_read_of_the_same_prefetched_lines_returns_them(tmp_path):
    """The pointer is sent once; a later read of the same lines gets the content."""
    path = _file(tmp_path, 10)
    prefetched, real = _whole_prefetch(path, 10)
    backend = ScriptedTurns(
        wants(("read_file", {"path": real, "start_line": 4, "end_line": 5})),
        wants(("read_file", {"path": real, "start_line": 4, "end_line": 5})),
        says("done"),
    )
    result = run(
        backend, allowed=frozenset({"read_file"}),
        cfg=_conf(tmp_path), prefetched=prefetched,
    )

    assert result.deduped == 2
    first, second = _all_results(backend)
    assert first.content == loop.PREFETCHED_POINTER.format(path=real)
    assert second.content.startswith(loop.PREFETCHED_REPEAT_PREFIX)
    assert " 4\tline 4" in second.content
    assert " 5\tline 5" in second.content


# --- the no-drift check ------------------------------------------------------------------


@posix_only
def test_the_seeded_content_slices_to_what_read_file_returns(tmp_path):
    """The seeded content must slice to exactly what `read_file` would have returned."""
    path = _file(tmp_path, 150)
    real = os.path.realpath(path)
    text = "".join(f"line {i}\n" for i in range(1, 151))
    pre = _entry(real, text, len(text.encode("utf-8")))
    entry = loop._prefetched_cache_entry(pre)
    call = ToolUseBlock(
        id="x", name="read_file",
        input={"path": real, "start_line": 10, "end_line": 20},
    )
    sliced = loop._slice_lines(call, entry.content)
    got = tools.execute_tool(_conf(tmp_path), call, frozenset({"read_file"}))
    assert not got.is_error
    assert sliced == got.content


@posix_only
@pytest.mark.parametrize(("start", "end", "sub"), [
    (2, 3, (2, 3)),        # a range ending on a blank line, the shape #421 fixed
    (None, None, (3, 4)),  # the whole file, sliced to a part holding the blank line
])
def test_a_real_prefetch_seeds_what_read_file_returns(tmp_path, start, end, sub):
    """The same no-drift check, with the entry built by `context.prefetch` itself.

    A hand-built `FileEntry` can only agree with the seeding code about its own shape; the
    prefetch's text for a range ending on a blank line ends in a newline, which is where a
    line count goes wrong.
    """
    p = tmp_path / "blank.py"
    p.write_text("a\nb\n\nc\n", encoding="utf-8")
    resolved = ResolvedPath(given=str(p), posix=os.path.realpath(p))
    request = context.FileRequest(entry=resolved, start_line=start, end_line=end)
    (pre,) = context.prefetch(_conf(tmp_path), (request,)).files
    entry = loop._prefetched_cache_entry(pre)
    call = ToolUseBlock(
        id="x", name="read_file",
        input={"path": pre.path, "start_line": sub[0], "end_line": sub[1]},
    )
    got = tools.execute_tool(_conf(tmp_path), call, frozenset({"read_file"}))
    assert not got.is_error
    assert entry.span == ((start, end) if start is not None else (1, 4))
    assert loop._slice_lines(call, entry.content) == got.content
