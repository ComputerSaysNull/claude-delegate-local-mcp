"""A read of lines already returned ran the tool again.

`read_file(path=X, start_line=10, end_line=20)` after `read_file(path=X, start_line=1,
end_line=100)` misses the dedup cache. Dedup keys on the whole argument dict, and the two
ranges differ, so the second call executes the tool again and the turn is charged in full
for bytes the history already holds -- the waste the cache exists to prevent, applied to
the one tool that pages.

The fix gives `read_file` range coverage: a call whose requested lines are a subset of an
earlier read of the same path is served from that earlier result, sliced, as a repeat.
Everything else keeps exact-argument dedup, a write still invalidates the cache, and a
truncated read covers only the lines it actually returned.
"""

from __future__ import annotations

import os

import pytest

from claude_delegate_local import context, loop
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.tools import BashPolicy

from test_agentic_loop import ScriptedTurns, cfg, results_in, run, says, wants

# Every case that reads a real file needs a POSIX filesystem: on Windows the path policy
# refuses anything under `tmp_path`, so each read fails and a "ran twice" case passes for
# the wrong reason.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Reading a real file "
    "needs a POSIX filesystem; run this file in WSL.",
)


def _file(tmp_path, n):
    p = tmp_path / "n.py"
    p.write_text("".join(f"line {i}\n" for i in range(1, n + 1)), encoding="utf-8")
    return str(p)


def _conf(tmp_path, **over):
    kw = {"workspace_roots": (str(tmp_path),)}
    kw.update(over)
    return cfg(**kw)


def _all_results(backend):
    """Every tool result the loop sent back, in the order it sent them."""
    return [r for req in backend.requests for r in results_in(req)]


# --- coverage ----------------------------------------------------------------------------


@posix_only
def test_a_range_of_lines_already_returned_is_served_without_running(tmp_path):
    """The bug. Lines 10-20 were part of the 1-100 read already in the history."""
    path = _file(tmp_path, 150)
    backend = ScriptedTurns(
        wants(("read_file", {"path": path, "start_line": 1, "end_line": 100})),
        wants(("read_file", {"path": path, "start_line": 10, "end_line": 20})),
        says("done"),
    )
    result = run(backend, allowed=frozenset({"read_file"}), cfg=_conf(tmp_path))

    assert result.deduped == 1
    served = _all_results(backend)[1]
    assert served.content.startswith(loop.REPEAT_PREFIX)
    body = served.content[len(loop.REPEAT_PREFIX):]
    lines = body.split("\n")
    assert len(lines) == 11
    assert lines[0] == " 10\tline 10"
    assert lines[-1] == " 20\tline 20"


@posix_only
def test_a_range_sticking_out_past_what_was_read_runs_again(tmp_path):
    """The other direction. Coverage that matched too widely answers the wrong question."""
    path = _file(tmp_path, 150)
    backend = ScriptedTurns(
        wants(("read_file", {"path": path, "start_line": 1, "end_line": 100})),
        wants(("read_file", {"path": path, "start_line": 90, "end_line": 120})),
        says("done"),
    )
    result = run(backend, allowed=frozenset({"read_file"}), cfg=_conf(tmp_path))

    assert result.deduped == 0
    served = _all_results(backend)[1]
    assert not served.content.startswith(loop.REPEAT_PREFIX)
    assert " 90\tline 90" in served.content


@posix_only
def test_a_whole_file_read_covers_a_later_range(tmp_path):
    """No `end_line` reads to the end, so a later range is already known."""
    path = _file(tmp_path, 10)
    backend = ScriptedTurns(
        wants(("read_file", {"path": path})),
        wants(("read_file", {"path": path, "start_line": 3, "end_line": 5})),
        says("done"),
    )
    result = run(backend, allowed=frozenset({"read_file"}), cfg=_conf(tmp_path))

    assert result.deduped == 1
    served = _all_results(backend)[1]
    assert served.content.startswith(loop.REPEAT_PREFIX)
    body = served.content[len(loop.REPEAT_PREFIX):]
    assert body == " 3\tline 3\n 4\tline 4\n 5\tline 5"


# --- what does not cover -------------------------------------------------------------


@posix_only
def test_a_batch_serves_calls_an_earlier_turn_already_read(tmp_path):
    """`_run_group` applies the same rule: a call in a batch covered by a prior read does
    not run, and the duplicate's sibling is served from it rather than dispatched."""
    path = _file(tmp_path, 150)
    backend = ScriptedTurns(
        wants(("read_file", {"path": path, "start_line": 1, "end_line": 100})),
        wants(("read_file", {"path": path, "start_line": 10, "end_line": 20}),
              ("read_file", {"path": path, "start_line": 30, "end_line": 40})),
        says("done"),
    )
    result = run(backend, allowed=frozenset({"read_file"}), cfg=_conf(tmp_path))

    assert result.deduped == 2
    served = _all_results(backend)
    assert len(served) == 3
    assert served[1].content.startswith(loop.REPEAT_PREFIX)
    assert served[2].content.startswith(loop.REPEAT_PREFIX)


@posix_only
def test_a_truncated_read_covers_only_the_lines_it_returned(tmp_path):
    """A read cut short by the character budget knows nothing past where it stopped."""
    path = _file(tmp_path, 150)
    backend = ScriptedTurns(
        wants(("read_file", {"path": path, "start_line": 1, "end_line": 100})),
        wants(("read_file", {"path": path, "start_line": 90, "end_line": 100})),
        says("done"),
    )
    result = run(
        backend,
        allowed=frozenset({"read_file"}),
        cfg=_conf(tmp_path, max_read_chars=200),
    )

    first = _all_results(backend)[0]
    assert "[truncated:" in first.content, "the budget must actually truncate here"
    assert result.deduped == 0
    served = _all_results(backend)[1]
    assert not served.content.startswith(loop.REPEAT_PREFIX)


@posix_only
def test_a_write_between_two_reads_makes_the_second_run_again(tmp_path):
    """Any non-cacheable call clears the cache, since a write invalidates earlier reads."""
    path = _file(tmp_path, 150)
    out = str(tmp_path / "out.txt")
    backend = ScriptedTurns(
        wants(("read_file", {"path": path, "start_line": 1, "end_line": 100})),
        wants(("write_file", {"path": out, "content": "x"})),
        wants(("read_file", {"path": path, "start_line": 10, "end_line": 20})),
        says("done"),
    )
    result = run(
        backend,
        allowed=frozenset({"read_file", "write_file"}),
        cfg=_conf(tmp_path),
    )

    assert result.deduped == 0
    served = _all_results(backend)[2]
    assert not served.content.startswith(loop.REPEAT_PREFIX)


def test_a_reread_covered_by_an_evicted_result_serves_the_notice():
    """ADR-0080, extended to range coverage: an evicted entry answers as a repeat but does
    not hand back content that was dropped from the history."""
    cached = {
        ("read_file", '{"end_line": 100, "path": "n.py", "start_line": 1}'): loop._CachedResult(
            "  1\tline 1", "call_1", evicted=True, path="n.py", span=(1, 100)
        ),
    }
    call = ToolUseBlock(
        id="call_2", name="read_file",
        input={"path": "n.py", "start_line": 10, "end_line": 20},
    )
    block, outcome, _ = loop._run_one_call(
        cfg(workspace_roots=(".",)), call, frozenset(), cached, BashPolicy()
    )
    assert outcome == "repeat"
    assert block.content == loop.EVICTED_REPEAT


# --- the parser ---------------------------------------------------------------------


def test_numbered_span_ignores_the_truncation_note():
    """The span is what the result *holds*, never what it asked for."""
    body = (
        "  1\tline 1\n  2\tline 2\n\n"
        "[truncated: lines 1 to 2 of 150. Call read_file again with start_line=3 "
        "for the rest.]"
    )
    assert context.numbered_span(body) == (1, 2)
