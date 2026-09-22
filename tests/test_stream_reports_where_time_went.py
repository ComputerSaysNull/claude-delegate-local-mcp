"""The stream says where a run's time went and what it did, not only that it ended.

A run summary wants five things the transcript could not supply. The `end` event carried
no tool counts at all -- `tool_calls`, `tool_errors`, `bash_calls` and `bash_failures`
reached only the per-dispatch `.json`, which is written once the work is over, while the
stream is the file anything following a run reads. And of the backend interval it carried
only the total: prefill was measured nowhere, and the decode span was computed and then
spent entirely on the rate estimator.

Tool time is a field here and deliberately not one on a turn event. There it is
`ms - backend_ms`, two numbers already present. Here the obvious subtraction --
`elapsed_seconds - backend_ms` -- is tool time *plus* the wait for an admission slot,
which happens before any turn runs, so rendering it as "tools" would show a queued
delegation as an expensive toolset. The last test in this file is that distinction.

The reconciliation is the one check here at risk of being unable to fail, so it is
asserted in both directions: it passes on a real run and fires on a fixture built to
break it.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from test_server import chat_reply, tool_call_reply
from test_transcript_stream import _run, two_turns
from wire_double import as_stream

from claude_delegate_local import loop, server

TASK = {"task": "explain the retry"}

# The idle-gate debounce, off. It is ten seconds of deliberate waiting before the first
# turn, which would dominate every timing here and slow the suite for nothing -- and,
# worse, would make the final test pass whether or not the queue is being charged to the
# tools, because the gap would be there either way.
FAST = {"admission_idle_hold": 0}


def _turns(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("t") == "turn"]


def _end(events: list[dict]) -> dict:
    ends = [e for e in events if e.get("t") == "end"]
    assert len(ends) == 1, f"expected one end event, found {len(ends)}"
    return ends[0]


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> tuple[list[dict], Path]:
    """One delegation, shared by every assertion that only reads its transcript.

    Module-scoped because these tests differ in what they assert, not in what they run,
    and a delegation apiece would pay for the same run five times.
    """
    directory = tmp_path_factory.mktemp("transcripts")
    return _run(directory, two_turns, "delegate", TASK, **FAST), directory


# ---- the turn event ------------------------------------------------------------------


def test_a_turn_event_splits_its_backend_time(run) -> None:
    """`backend_ms` alone says how long, never where it went.

    Prefill grows with the prompt and decode with the answer, so a run that is slow
    because it is re-sending a large history and one that is slow because it is writing
    a long reply were the same line in this file, under two opposite remedies.
    """
    events, _ = run
    turns = _turns(events)
    assert turns, "the run produced no turn events to assert on"

    for event in turns:
        assert "prefill_seconds" in event, (
            f"turn {event.get('turn')} reports {sorted(event)} and no prefill. The "
            "request-to-first-token interval is measured in the adapter and thrown away."
        )
        assert "decode_seconds" in event, (
            f"turn {event.get('turn')} reports {sorted(event)} and no decode span. It "
            "is computed for the rate estimator and reaches no event."
        )


# ---- the end event -------------------------------------------------------------------


def test_the_end_event_carries_the_tool_counts(run) -> None:
    """A reader following the stream could see every turn and not one tool call."""
    end = _end(run[0])

    for key in ("tool_calls", "tool_errors", "bash_calls", "bash_failures"):
        assert key in end, (
            f"the end event reports {sorted(end)}, with no {key}. The count exists in "
            "the per-dispatch record, which is written after the run this event ends."
        )


def test_the_end_counts_are_the_records_counts(run) -> None:
    """One computation, not two.

    Counting again for the stream's benefit is how two files come to disagree about one
    delegation -- and the disagreement would be invisible, because nobody reads both.
    """
    events, directory = run
    end = _end(events)
    records = sorted(directory.glob("*.json"))
    assert len(records) == 1, f"expected one record, found {[p.name for p in records]}"
    record = json.loads(records[0].read_text(encoding="utf-8"))

    for key in ("tool_calls", "tool_errors", "bash_calls", "bash_failures"):
        assert end[key] == record[key], (
            f"the stream says {key}={end[key]} and the record says {record[key]} about "
            "the same delegation, so they are counting it twice in two places"
        )
    assert record["tool_calls"] >= 1, (
        "the fixture made no tool calls, so equal counts prove nothing"
    )


def test_the_end_event_carries_all_three_spans(run) -> None:
    """The same split as the turn events, over the run, plus the tools."""
    end = _end(run[0])
    for key in ("prefill_seconds", "decode_seconds", "tool_seconds"):
        assert key in end, f"the end event reports {sorted(end)}, with no {key}"


# ---- reconciliation, in both directions ------------------------------------------------

# How far the accounted time may overshoot the wall clock before the accounting is wrong
# rather than merely imprecise. The spans are read from two clocks at different moments,
# so this is a check on the arithmetic, not on the timers.
TOLERANCE_SECONDS = 1.0


def _overspend(events: list[dict]) -> float:
    """Accounted seconds minus the wall clock. Positive means the accounting is wrong.

    All three spans come from the `end` event. Nothing here may exceed `elapsed_seconds`,
    which contains all of them plus the wait for an admission slot.
    """
    end = _end(events)
    accounted = (
        (end.get("prefill_seconds") or 0.0)
        + (end.get("decode_seconds") or 0.0)
        + (end.get("tool_seconds") or 0.0)
    )
    return accounted - (end.get("elapsed_seconds") or 0.0)


def test_the_spans_reconcile_against_the_elapsed_clock(run) -> None:
    """Prefill plus decode plus tool time fits inside the run, or one of them is wrong."""
    over = _overspend(run[0])
    assert over <= TOLERANCE_SECONDS, (
        f"the events account for {over:.3f}s more than the delegation took. Either a "
        "span is being summed twice or one of them is not the interval it is named for."
    )


def test_the_reconciliation_fires_on_a_run_that_does_not_reconcile(run) -> None:
    """The other half, and the reason the test above is worth anything.

    A check over three fields that are all small and all optional passes on almost any
    input, including on a version of the code that never filled them in. So the same
    checker is pointed at a fixture built to break it, and must say so. This repository
    has found six checks that could not fail; that is what this is here to prevent.
    """
    events = json.loads(json.dumps(run[0]))
    end = _end(events)

    # One delegation's decode span claimed to be ten times the delegation. Chosen rather
    # than an absent field because absent is the *unfixed* shape, and a checker that
    # fires on it would be reporting the feature's absence instead of a mismatch.
    end["decode_seconds"] = (end["elapsed_seconds"] or 0.0) * 10 + 60.0

    over = _overspend(events)
    assert over > TOLERANCE_SECONDS, (
        f"the checker accepted a run claiming {end['decode_seconds']:.1f}s of decoding "
        f"inside {end['elapsed_seconds']:.3f}s, overspending by {over:.3f}s. It cannot "
        "fail, so its passing above said nothing."
    )


# ---- tool time, which is not elapsed minus backend --------------------------------------

TOOL_ONE = 0.25
TOOL_TWO = 0.45
GATE_WAIT = 0.60
# Real sleeps on a real clock, with three turns of bookkeeping between them. Wide enough
# that a loaded machine does not fail this, far narrower than what is being told apart.
SLACK = 0.25


def _two_tool_turns(request):
    """A tool call on the first two turns, an answer on the third."""
    body = json.loads(request.content)
    ran = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
    if ran >= 2:
        return as_stream(chat_reply(content="both tools ran"))
    return as_stream(tool_call_reply("read_file", {"path": "/nope.py"}))


def _sleeping_tools(monkeypatch) -> None:
    """Make the two tool-running turns take a known length of time each.

    Patched at `_run_calls`, the one place a turn's tools are executed, so the sleep
    lands inside the interval being measured rather than beside it.
    """
    real = loop._run_calls
    waits = iter((TOOL_ONE, TOOL_TWO))

    def slow(*args, **kwargs):
        time.sleep(next(waits, 0.0))
        return real(*args, **kwargs)

    monkeypatch.setattr(loop, "_run_calls", slow)


def _queue_for(monkeypatch, seconds: float) -> None:
    """Make every delegation wait at the gate, which an idle one never does."""

    class SlowGate(server.Admission):
        @asynccontextmanager
        async def admit(self, *args, **kwargs):
            await asyncio.sleep(seconds)
            async with super().admit(*args, **kwargs) as lease:
                yield lease

    monkeypatch.setattr(server, "Admission", SlowGate)


def test_tool_time_is_the_tools_and_not_the_queue(tmp_path, monkeypatch) -> None:
    """Two runs, identical but for a queue, and only one of the two figures moves.

    `elapsed_seconds - backend_ms` is the obvious way to get tool time and is wrong: it
    also contains the wait for an admission slot, which happens before any turn runs. On
    an idle gate the two agree, which is exactly why one run cannot show this -- a single
    reading would pass whether or not the wait were being charged to the tools. So the
    same delegation is run twice, and the assertion is on what changes between them.
    """
    def once(directory: Path, queue: float) -> dict:
        with monkeypatch.context() as patch:
            _sleeping_tools(patch)
            if queue:
                _queue_for(patch, queue)
            return _end(_run(directory, _two_tool_turns, "delegate", TASK, **FAST))

    idle = once(tmp_path / "idle", 0.0)
    queued = once(tmp_path / "queued", GATE_WAIT)

    expected = TOOL_ONE + TOOL_TWO
    for name, end in (("idle", idle), ("queued", queued)):
        assert end["tool_seconds"] == pytest.approx(expected, abs=SLACK), (
            f"the {name} run's tools slept {expected}s and the event reports "
            f"{end['tool_seconds']}s"
        )

    naive_idle = idle["elapsed_seconds"] - (idle["backend_ms"] or 0) / 1000
    naive_queued = queued["elapsed_seconds"] - (queued["backend_ms"] or 0) / 1000

    assert naive_queued - naive_idle > GATE_WAIT / 2, (
        f"elapsed minus backend was {naive_idle:.3f}s idle and {naive_queued:.3f}s "
        f"behind a {GATE_WAIT}s queue. If a queue does not move it, this test is not "
        "queueing and proves nothing about the two figures."
    )
    assert abs(queued["tool_seconds"] - idle["tool_seconds"]) < GATE_WAIT / 2, (
        f"tool_seconds went from {idle['tool_seconds']}s to "
        f"{queued['tool_seconds']}s for the same tools, so the wait for a slot is "
        "being reported as time spent running them"
    )
