"""The viewer showed one failed shell call as "2 failures".

Its summary added `tool_errors` and `bash_failures`, and a `run_bash` that exits non-zero is
in both, so every failing shell call was counted twice; the comment beside the sum said the
overlap was only a refused call. Neither counter alone is right either -- a masked failure
reaches `bash_failures` without being an error -- so the server now counts `failed_calls`,
each call once, and the viewer shows that.
"""

from __future__ import annotations

from claude_delegate_local.backends.base import BashOutcome, ToolResultBlock, ToolUseBlock
from claude_delegate_local.loop import _Watch

from test_watch_delegations import load_viewer


def _call(n: int, name: str) -> ToolUseBlock:
    return ToolUseBlock(id=f"call-{n}", name=name, input={"command": "x"})


def _result(n: int, outcome: BashOutcome | None = None) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=f"call-{n}", content="", bash=outcome)


def test_each_failing_call_is_counted_once_whatever_way_it_failed() -> None:
    watch = _Watch(diagnostics=False)
    # A shell call that exited 2: an error and a shell failure at once.
    watch.called(_call(1, "run_bash"), "error", _result(1, BashOutcome(exit_code=2, ran=True)))
    # A masked failure: exit 0, so not an error, but a shell failure.
    watch.called(_call(2, "run_bash"), "ran",
                 _result(2, BashOutcome(exit_code=0, ran=True, masked_failure=True)))
    # A refused read: an error with no shell in it.
    watch.called(_call(3, "read_file"), "error")
    # And one that simply worked.
    watch.called(_call(4, "run_bash"), "ran", _result(4, BashOutcome(exit_code=0, ran=True)))

    assert (watch.tool_errors, watch.bash_failures) == (2, 2), "the two overlapping counters"
    assert watch.failed_calls == 3, "three calls failed, each in one way or two"


def test_the_viewer_shows_the_distinct_count() -> None:
    viewer = load_viewer()
    end = {"t": "end", "tool_calls": 11, "tool_errors": 1, "bash_calls": 9,
           "bash_failures": 1, "failed_calls": 1}
    line = " ".join(viewer._end_counts(end))
    assert "1 failure" in line and "2 failures" not in line, line


def test_a_transcript_from_before_the_counter_still_shows_the_old_sum() -> None:
    """Absent is not zero: an older end event has no `failed_calls`, so the viewer keeps the
    sum it always showed rather than claiming a count nobody made."""
    viewer = load_viewer()
    end = {"t": "end", "tool_calls": 11, "tool_errors": 1, "bash_calls": 9, "bash_failures": 1}
    assert "2 failures" in " ".join(viewer._end_counts(end))
