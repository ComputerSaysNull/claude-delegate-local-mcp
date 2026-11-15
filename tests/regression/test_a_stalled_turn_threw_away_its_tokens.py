"""A stalled turn's decoded tokens were thrown away, and its last partial never written.

Two separate losses, both on the failure path only:

`complete_with_retry` copies a decoded partial onto the `DispatchTimedOut` it builds from a
`TimeoutError` *after* asking which clock expired. `stalled()` raises its own fresh
`DispatchTimedOut` from inside that check, so a turn that streamed thousands of tokens and
then went quiet -- exactly the case ADR-0078 exists for -- is reported with no partial at
all. The retry path does the same: a failure that carried a partial, met by a stall, loses
it in `stalled()` and in the "while waiting to retry" raise beside it.

The partial writer is the second loss. `PartialText.flush()` runs only where a turn
*answered*: after `dispatch_with_recovery` in `run_one_shot`, and in the agentic loop's
`turn_done`. A turn that raised never reached either, so the text it had already streamed
was never closed with `final: true` and a reader could not tell it from a turn still
running.

Every test here is driven by a fake clock and injected `tick_sleep`, because the deadline
logic is the one place the loop reads real time: without them a test spends the budget it
is measuring, and the doubles below never advance the clock themselves.
"""

import asyncio

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import BackendUnavailable

from test_a_cancelled_turn_threw_away_its_tokens import _DiesHolding
from test_agentic_loop import cfg as cfg_agentic, entry as agentic_entry
from test_loop import FakeClock, cfg as cfg_loop, entry as one_shot_entry, ok_response, one_shot


def ticking(clock: FakeClock):
    """A `tick_sleep` that costs the fake clock what the watchdog asked to wait for."""
    async def tick_sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return tick_sleep


class _FailsRetryably:
    """Decodes something, then fails in the retryable way an adapter reports.

    It hangs the partial on the exception, as the adapter does, and spends `seconds` of the
    fake clock doing so -- which is how a turn that overran the stall reaches the retry
    logic rather than being cut by the watchdog.
    """

    def __init__(self, partial, clock: FakeClock, *, seconds: float = 0.0,
                 while_generating: bool = False) -> None:
        self.partial = partial
        self.clock = clock
        self.seconds = seconds
        self.while_generating = while_generating
        self.calls = 0

    async def complete(self, request, *, on_token=None):
        self.calls += 1
        self.clock.advance(self.seconds)
        failure = BackendUnavailable("dropped after the axe",
                                     while_generating=self.while_generating)
        failure.partial = self.partial
        raise failure


class _StreamsThen:
    """Reports `pieces` to the loop's partial writer, then answers or fails.

    Both halves matter: the answer proves a successful turn flushes exactly once, and the
    failure proves a turn that never answers still closes its stream.
    """

    def __init__(self, pieces, *, answer=None, partial=None) -> None:
        self.pieces = pieces
        self.answer = answer
        self.partial = partial

    async def complete(self, request, *, on_token=None):
        for piece in self.pieces:
            if on_token is not None:
                on_token("answer", piece)
        if self.answer is not None:
            return self.answer
        failure = BackendUnavailable("read timeout after the axe", while_generating=True)
        failure.partial = self.partial
        raise failure


# --- the partial a stall throws away -----------------------------------------------------


def test_the_stall_carries_the_partial_out_of_the_loop():
    """The stall clock fires first, and the tokens must ride out on the exception.

    `_DiesHolding` re-raises whatever ends it with the partial attached, which is what the
    adapter does; the watchdog cancels it once the stall deadline is gone. The raise that
    follows must not be a fresh `DispatchTimedOut` with nothing on it.
    """
    clock = FakeClock()
    start = clock()
    kept = ok_response("everything it managed before the axe")

    with pytest.raises(loop.DispatchTimedOut) as caught:
        asyncio.run(
            loop.complete_with_retry(
                cfg_loop(dispatch_timeout=100, stall_timeout=2, connect_timeout=1,
                         retry_max_attempts=1),
                _DiesHolding(kept),
                one_shot("hello"),
                deadline=start + 100,
                stall_left=lambda: 2.0 - (clock() - start),
                tick_sleep=ticking(clock),
                clock=clock,
            )
        )

    assert caught.value.stalled is True, "the stall clock was the one that expired"
    assert caught.value.partial is kept, "the stalled turn reported none of what it decoded"


def test_both_clocks_expiring_together_keep_the_partial():
    """A stall that lands on the delegation deadline must not cost the tokens either.

    Which diagnosis wins decides the message, never whether the decoded text is returned.
    """
    clock = FakeClock()
    start = clock()
    kept = ok_response("decoded, then abandoned")

    with pytest.raises(loop.DispatchTimedOut) as caught:
        asyncio.run(
            loop.complete_with_retry(
                cfg_loop(dispatch_timeout=2, stall_timeout=2, connect_timeout=1,
                         retry_max_attempts=1),
                _DiesHolding(kept),
                one_shot("hello"),
                deadline=start + 2,
                stall_left=lambda: 2.0 - (clock() - start),
                tick_sleep=ticking(clock),
                clock=clock,
            )
        )

    assert caught.value.partial is kept


def test_the_stall_at_the_retry_carries_the_partial():
    """A retryable failure that overran the stall: the retry path's `stalled()` raise.

    The backend hands over a partial and spends three seconds doing it, so the stall clock
    is already gone when the retry logic reads it. The delegation ends there, and what the
    attempt decoded has to end up on the exception.
    """
    clock = FakeClock()
    start = clock()
    kept = ok_response("streamed, then the route dropped")
    backend = _FailsRetryably(kept, clock, seconds=3.0)

    with pytest.raises(loop.DispatchTimedOut) as caught:
        asyncio.run(
            loop.complete_with_retry(
                cfg_loop(dispatch_timeout=100, stall_timeout=2, connect_timeout=1,
                         retry_max_attempts=9, retry_base_delay=1.0),
                backend,
                one_shot("hello"),
                deadline=clock() + 100,
                # Open at entry, gone by the time the attempt hands its failure over.
                stall_left=lambda: 2.0 - (clock() - start),
                jitter=lambda lo, hi: hi,
                clock=clock,
            )
        )

    assert caught.value.stalled is True
    assert caught.value.partial is kept


def test_a_retry_wait_that_outlasts_the_stall_carries_the_partial():
    """The other raise in the retry path: a wait too long for what is left.

    Here the stall clock has not quite gone, so `stalled()` stays silent and the
    "while waiting to retry" deadline is raised instead. It is a different message and the
    same loss unless the partial is carried.
    """
    clock = FakeClock()
    start = clock()
    kept = ok_response("half a reply, then a dropped route")
    backend = _FailsRetryably(kept, clock, seconds=0.5)

    with pytest.raises(loop.DispatchTimedOut) as caught:
        asyncio.run(
            loop.complete_with_retry(
                cfg_loop(dispatch_timeout=100, stall_timeout=2, connect_timeout=1,
                         retry_max_attempts=9, retry_base_delay=2.0),
                backend,
                one_shot("hello"),
                deadline=clock() + 100,
                stall_left=lambda: 2.0 - (clock() - start),
                jitter=lambda lo, hi: hi,
                clock=clock,
            )
        )

    assert caught.value.stalled is False, "this one is the wait, not the stall"
    assert "waiting to retry" in str(caught.value)
    assert caught.value.partial is kept


# --- the final partial a failed turn never wrote -----------------------------------------


def test_a_failed_one_shot_turn_still_writes_its_final_partial():
    """The one-shot path flushes after a *successful* dispatch only.

    Two pieces arrive inside one interval, so the second is still held when the turn dies.
    Without the flush it is never written at all, and the partials a watcher saw end on
    `final: false` -- the shape of a turn still running.
    """
    partials: list = []
    kept = ok_response("the first half and the second")
    backend = _StreamsThen(["the first half", " and the second"], partial=kept)

    with pytest.raises(BackendUnavailable):
        asyncio.run(
            loop.run_one_shot(
                cfg_loop(partial_every_seconds=3600.0, retry_max_attempts=1,
                         dispatch_timeout=100),
                one_shot_entry(),
                backend,
                loop.Delegation("hello"),
                on_partial=lambda turn, reasoning, answer, final: partials.append(
                    (turn, reasoning, answer, final)),
                clock=FakeClock(),
            )
        )

    assert partials, "the failed turn wrote no partial at all"
    assert "".join(answer for _, _, answer, _ in partials) == kept.text
    assert partials[-1][3] is True, "the last partial did not close the turn's stream"


def test_a_successful_one_shot_turn_flushes_exactly_once():
    """The negative control for the flush above.

    A flush in a `finally` that also runs on the answering path would write a second
    `final: true` -- an empty one, after the text it already closed.
    """
    partials: list = []
    backend = _StreamsThen(["the first half", " and the second"],
                           answer=ok_response("the first half and the second"))

    asyncio.run(
        loop.run_one_shot(
            cfg_loop(partial_every_seconds=3600.0, dispatch_timeout=100),
            one_shot_entry(),
            backend,
            loop.Delegation("hello"),
            on_partial=lambda turn, reasoning, answer, final: partials.append(
                (turn, reasoning, answer, final)),
            clock=FakeClock(),
        )
    )

    finals = [p for p in partials if p[3]]
    assert len(finals) == 1, partials
    assert "".join(answer for _, _, answer, _ in partials) == "the first half and the second"


def test_a_failed_agentic_turn_still_writes_its_final_partial():
    """The agentic loop flushes in `turn_done`, which a turn that raises never reaches."""
    partials: list = []
    kept = ok_response("the first half and the second")
    backend = _StreamsThen(["the first half", " and the second"], partial=kept)

    with pytest.raises(BackendUnavailable):
        asyncio.run(
            loop.run_agentic_loop(
                cfg_agentic(partial_every_seconds=3600.0, retry_max_attempts=1,
                            dispatch_timeout=100),
                agentic_entry(),
                backend,
                loop.Delegation("hello"),
                allowed=frozenset(), max_turns=2,
                on_partial=lambda turn, reasoning, answer, final: partials.append(
                    (turn, reasoning, answer, final)),
                clock=FakeClock(),
            )
        )

    assert partials, "the failed turn wrote no partial at all"
    assert "".join(answer for _, _, answer, _ in partials) == kept.text
    assert partials[-1][3] is True, "the last partial did not close the turn's stream"
    assert partials[-1][0] == 1, "the partial belongs to the turn that wrote it"


def test_a_successful_agentic_turn_flushes_exactly_once():
    """As above for the turn loop: `turn_done` flushes, and nothing may flush again."""
    partials: list = []
    backend = _StreamsThen(["the first half", " and the second"],
                           answer=ok_response("the first half and the second"))

    asyncio.run(
        loop.run_agentic_loop(
            cfg_agentic(partial_every_seconds=3600.0, dispatch_timeout=100),
            agentic_entry(),
            backend,
            loop.Delegation("hello"),
            allowed=frozenset(), max_turns=1,
            on_partial=lambda turn, reasoning, answer, final: partials.append(
                (turn, reasoning, answer, final)),
            clock=FakeClock(),
        )
    )

    finals = [p for p in partials if p[3]]
    assert len(finals) == 1, partials
    assert "".join(answer for _, _, answer, _ in partials) == "the first half and the second"


def test_a_second_flush_of_one_turn_writes_no_second_final():
    """A turn closed once stays closed. The agentic loop now flushes on every failure, and a
    failure can land after a successful turn already flushed in `turn_done` -- a tool that
    raised, say -- before the next turn's `reset`. A second `final: true` would tell a reader
    the turn ended twice, the second time with nothing in it."""
    emitted: list[tuple[str, str, bool]] = []
    text = loop.PartialText(1.0, lambda r, a, f: emitted.append((r, a, f)), clock=lambda: 0.0)
    text.add("answer", "the whole reply")
    text.flush()
    text.flush()
    finals = [e for e in emitted if e[2]]
    assert len(finals) == 1, f"one turn wrote {len(finals)} final partials: {finals}"
