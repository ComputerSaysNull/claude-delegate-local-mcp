"""The heartbeat says whether the model is producing, not only that time is passing.

`alive` exists because a one-shot writes nothing between `start` and `end`, so a delegation
working perfectly is indistinguishable from one whose server was killed. It carried elapsed
and a deadline and deliberately nothing about the model, because there was no streaming and
the server genuinely did not know (ADR-0018).

It does now (ADR-0072). What it reports is **chunks**, and the naming is the point: a frame
usually carries one token on this stack and is not promised to, and the only real token
count arrives in the final usage frame, after the heartbeat has stopped mattering.
"""

from __future__ import annotations

import pytest
from wire_double import Clock, delta, paced

import scripts.watch_delegations as wd

pytestmark = pytest.mark.anyio


def _alive(**over) -> dict:
    event = {
        "t": "alive", "at": "2026-09-13T10:00:00+00:00",
        "elapsed_seconds": 41.0, "of_seconds": 3600, "ends_in_seconds": 120.0,
        "chunks_seen": 0, "since_chunk_seconds": None,
    }
    event.update(over)
    return event


def test_the_viewer_shows_what_has_arrived():
    line = wd._alive_line(_alive(chunks_seen=3210, since_chunk_seconds=0.2))
    assert "3,210 chunks" in line
    # Named for what was counted. Calling frames "tokens" would be a guess presented as a
    # measurement, which is exactly what this event's docstring used to refuse to do.
    assert "token" not in line


def test_a_recent_chunk_is_not_worth_reporting_but_a_stale_one_is():
    """The gap is the half that separates producing from gone quiet.

    Both directions, because reporting the gap always would bury it in noise on a healthy
    stream, and never reporting it would lose the one case a reader is watching for.
    """
    flowing = wd._alive_line(_alive(chunks_seen=900, since_chunk_seconds=0.3))
    stalled = wd._alive_line(_alive(chunks_seen=900, since_chunk_seconds=47.0))

    assert "ago" not in flowing, "a live stream should not be annotated with its own latency"
    assert "last 47s ago" in stalled


def test_nothing_arrived_reads_as_it_did_before():
    """A delegation still prefilling has produced nothing, and must not claim otherwise."""
    line = wd._alive_line(_alive(chunks_seen=0, since_chunk_seconds=None))
    assert "chunks" not in line
    assert "still running" in line


async def test_the_count_comes_from_frames_that_carried_output():
    """End to end from the wire, so the number is not merely plumbed but correct.

    The negative control is inside the schedule: a role preamble and a finish frame are
    delivered alongside three real deltas, and counting frames rather than *generated*
    frames would report five.
    """
    import json

    from test_backends_openai_compat import backend, request

    clock = Clock()
    seen = 0

    def count(_kind: str) -> None:
        nonlocal seen
        seen += 1

    schedule = [
        (1.0, "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}) + "\n\n"),
        (5.0, "data: " + json.dumps(delta(content="a")) + "\n\n"),
        (6.0, "data: " + json.dumps(delta(content="b")) + "\n\n"),
        (7.0, "data: " + json.dumps(delta(content="c")) + "\n\n"),
        (7.0, "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 9, "completion_tokens": 3}}) + "\n\n"),
        (7.0, "data: [DONE]\n\n"),
    ]

    await backend(paced(clock, schedule), clock=clock).complete(request(), on_token=count)

    assert seen == 3, "only frames carrying generated output count as arrivals"
