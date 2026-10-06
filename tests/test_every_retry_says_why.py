"""Every extra attempt in one turn says why it happened.

`turn.retries` is the one place a reader learns why a turn took more than one attempt.
`complete_with_retry` fills it for transport failures, but the recovery ladder in
`dispatch_with_recovery` (ADR-0014) also sends again -- at a larger budget and at a stepped
down effort -- for a reply that came back empty at a length stop, and those resends added
to `attempts` while recording nothing. Measured over real transcripts: 82 of 88 retried
turns had no record.

The ladder owns its retries, so the record it must leave is an `EmptyAtLength` one, written
the moment it moves on from the empty attempt. Each case below drives the ladder directly
with a scripted backend, so the count of records against the count of attempts is the one
invariant no individual case can miss.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from claude_delegate_local import loop, transcript
from claude_delegate_local.backends.base import BackendUnavailable
from test_loop import (
    FakeClock,
    ScriptedBackend,
    SleepSpy,
    SlowBackend,
    cfg,
    empty_at_length,
    entry,
    ok_response,
)


def _dispatch(backend, *, config=None, entry_over=None, clock=None, effort="low"):
    """`dispatch_with_recovery` with the boring arguments filled in."""
    kw = {"sleep": SleepSpy()}
    if clock is not None:
        kw["clock"] = clock
    return asyncio.run(
        loop.dispatch_with_recovery(
            config or cfg(),
            entry(**(entry_over or {})),
            backend,
            lambda level, budget: loop.build_one_shot_request(
                delegation=loop.Delegation("x"), effort=level,
                max_tokens=budget, temperature=1.0, top_p=1.0,
            ),
            effort=effort,
            deadline=None,
            **kw,
        )
    )


def test_the_budget_retry_records_the_empty_attempt():
    """First reply empty at a length stop, the floor retry answers. The ladder moved on
    from one empty attempt, so it must have left one record, carrying that attempt's run
    time -- the same number `complete_with_retry` reports for a transport failure."""
    clock = FakeClock()
    backend = SlowBackend([empty_at_length(), ok_response("answered")], clock, seconds=7.0)
    dispatch = _dispatch(
        backend,
        config=cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="low"),
        clock=clock,
    )
    assert dispatch.attempts == 2
    assert [r.kind for r in dispatch.retries] == ["EmptyAtLength"]
    assert len(dispatch.retries) == dispatch.attempts - 1
    assert dispatch.retries[0].status is None
    assert dispatch.retries[0].wait == 0.0
    assert dispatch.retries[0].seconds == 7.0


def test_the_step_down_records_the_empty_attempt_the_budget_retry_skipped():
    """The model's cap pinned the first budget, so stage two was skipped and the ladder
    went straight to the step-down. The empty attempt it moved on from is still a retry
    and still has to be recorded."""
    backend = ScriptedBackend([empty_at_length(), ok_response("stepped")])
    dispatch = _dispatch(
        backend,
        config=cfg(max_tokens=100000, thinking_max_tokens_floor=50000, thinking_default="high"),
        entry_over={"max_tokens_cap": 4096},
        effort="high",
    )
    assert dispatch.attempts == 2
    assert [r.kind for r in dispatch.retries] == ["EmptyAtLength"]
    assert len(dispatch.retries) == dispatch.attempts - 1


def test_a_still_empty_stage_records_one_record_per_empty_attempt():
    """The budget retry also came back empty, so the step-down moved on from two empty
    attempts and both must be recorded."""
    backend = ScriptedBackend([empty_at_length(), empty_at_length(), ok_response("answered")])
    dispatch = _dispatch(
        backend,
        config=cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="high"),
        entry_over={"max_tokens_cap": 500000},
        effort="high",
    )
    assert dispatch.attempts == 3
    assert [r.kind for r in dispatch.retries] == ["EmptyAtLength", "EmptyAtLength"]
    assert len(dispatch.retries) == dispatch.attempts - 1


def test_a_transport_retry_and_an_empty_stage_are_both_recorded():
    """A turn with both kinds of extra attempt must show both: the dropped route and the
    empty-at-length resend are different failures with different remedies."""
    backend = ScriptedBackend(
        [BackendUnavailable("dropped"), empty_at_length(), ok_response("answered")]
    )
    dispatch = _dispatch(
        backend,
        config=cfg(
            retry_max_attempts=3, max_tokens=1000,
            thinking_max_tokens_floor=50000, thinking_default="low",
        ),
    )
    assert dispatch.attempts == 3
    assert [r.kind for r in dispatch.retries] == ["BackendUnavailable", "EmptyAtLength"]
    assert len(dispatch.retries) == dispatch.attempts - 1


def test_an_answer_on_the_first_try_records_nothing():
    """The negative control: no extra attempt, no record."""
    dispatch = _dispatch(ScriptedBackend([ok_response("done")]))
    assert dispatch.attempts == 1
    assert dispatch.retries == ()


def test_the_schema_enum_is_the_same_set_as_retry_kinds():
    """The shipped schema and the code name the same kinds, so a kind added to one side
    cannot silently fail to reach the other."""
    schema = json.loads(
        Path(transcript.__file__).with_name("transcript.schema.json").read_text(encoding="utf-8")
    )
    enum = schema["$defs"]["retry"]["properties"]["kind"]["enum"]
    assert set(enum) == set(loop.RETRY_KINDS)
