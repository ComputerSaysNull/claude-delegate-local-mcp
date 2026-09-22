"""A dispatch that called no tools reported seconds of tool time anyway.

Sighted in the viewer: a closing summary reading `2s tools` beside `0 tool calls`.
Two independent defects produced it, and either alone still shows the symptom.

The server's accumulator charged every turn's non-backend wall clock to tools, on the
assumption that whatever a turn spends outside the backend call is tool execution. That
holds only for a turn that ran tools; for one that ran none the bucket collects the
dispatch's own bookkeeping -- budget pricing, request assembly, transcript writes -- and
reports it under a name that says the model did something it did not.

The viewer then printed that figure whatever it was, while the per-turn renderer beside
it gates on the calls and documents why: "one that ran none says nothing rather than
`0s tools`". Gating the display alone would hide the symptom and leave the number wrong,
so both are pinned here.
"""

from __future__ import annotations

import pytest


class _Diagnostic:
    """The subset of a turn diagnostic the accumulator reads."""

    def __init__(self, *, tool_calls=(), output_tokens=0):
        self.tool_calls = tuple(tool_calls)
        self.output_tokens = output_tokens
        self.cached_tokens = None
        self.prefill_seconds = None
        self.decode_seconds = None


def _accumulate(turns, *, clock):
    """Replay the server's tool-time accumulation over a sequence of turns.

    Mirrors `streamed_turn` in `server.py`: one monotonic clock started when the slot is
    granted, each turn adding what it spent outside its own backend call. Driven here
    rather than through `run_delegation`, which has no seam for a clock.
    """
    from claude_delegate_local.server import tool_ms_for_turn

    total = 0
    tool_clock = clock()
    for diagnostic, backend_seconds in turns:
        now = clock()
        total += tool_ms_for_turn(
            tool_clock, now, int(backend_seconds * 1000), diagnostic
        )
        tool_clock = now
    return total


def test_a_turn_that_ran_no_tools_adds_no_tool_time():
    """The defect, at its smallest: one turn, no tools, a second of server overhead."""
    ticks = iter([100.0, 101.0])  # granted, then a turn landing a second later
    total = _accumulate(
        [(_Diagnostic(tool_calls=()), 0.5)], clock=lambda: next(ticks)
    )

    assert total == 0, (
        "a turn that called no tools reported tool time; the half-second between the "
        f"backend call and the turn landing was charged to tools as {total}ms"
    )


def test_a_turn_that_ran_tools_still_reports_them():
    """The positive control. Gating on the calls must not silence a real measurement."""
    ticks = iter([100.0, 103.0])
    total = _accumulate(
        [(_Diagnostic(tool_calls=({"name": "read_file"},)), 1.0)],
        clock=lambda: next(ticks),
    )

    assert total == 2000, f"three seconds less a one-second call is 2000ms, got {total}"


def test_only_the_turns_that_ran_tools_contribute():
    """The mixed run, which is the shape a real dispatch takes: the toolless turns in
    between must not quietly inflate the total the summary line reports."""
    ticks = iter([0.0, 2.0, 5.0, 9.0])
    total = _accumulate(
        [
            (_Diagnostic(tool_calls=({"name": "read_file"},)), 1.0),  # 2s - 1s = 1000
            (_Diagnostic(tool_calls=()), 1.0),                        # excluded
            (_Diagnostic(tool_calls=({"name": "search_files"},)), 1.0),  # 4s - 1s = 3000
        ],
        clock=lambda: next(ticks),
    )

    assert total == 4000, (
        f"expected 1000ms + 3000ms from the two turns that ran tools, got {total}"
    )


@pytest.fixture()
def viewer():
    """The same import the viewer's own suite uses."""
    import importlib.util
    import pathlib

    path = (pathlib.Path(__file__).resolve().parents[2]
            / "scripts" / "watch_delegations.py")
    spec = importlib.util.spec_from_file_location("watch_delegations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_summary_line_does_not_claim_tool_time_with_no_tool_calls(viewer):
    """The viewer's half, and the negative control its per-turn sibling already has.

    `tool_calls` is an integer on an `end` event and a list on a `turn` event, so this
    asserts against the shape the summary actually receives.
    """
    line = " ".join(viewer._end_timings({
        "elapsed_seconds": 300.0, "tool_seconds": 2.0,
        "prefill_seconds": 3.0, "decode_seconds": 290.0,
        "tool_calls": 0,
    }))

    assert "tools" not in line, (
        f"the closing summary claimed tool time on a dispatch that called none: {line!r}"
    )
    assert "generating" in line, "the other durations must survive the gate"


def test_the_summary_line_still_reports_real_tool_time(viewer):
    """The positive control for the viewer half."""
    line = " ".join(viewer._end_timings({
        "elapsed_seconds": 300.0, "tool_seconds": 42.0,
        "prefill_seconds": 3.0, "decode_seconds": 250.0,
        "tool_calls": 7,
    }))

    assert "42s tools" in line, line
