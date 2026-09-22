"""A turn that died mid-stream discarded every token it had already decoded.

`_post_stream` reaches `acc.payload()` on exactly one path -- the normal return after
`[DONE]`. There is no `finally` and no `except` that returns it, so a turn killed by a
read timeout, by any other transport failure or by the caller's deadline dropped the
accumulator on the floor. Nine dispatches generated 265,092 tokens and answered with the
empty string.

Every assertion here is driven through a *lazy* body. A buffered `httpx.Response` hands
the adapter the whole stream before the clock can move, so the deadline under test never
fires and the test would pass against the unfixed code.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from claude_delegate_local import loop, server
from claude_delegate_local.backends import base

from test_backends_openai_compat import backend, cfg, request
from test_loop import FakeClock, cfg as cfg_loop, ok_response, one_shot
from wire_double import Clock


class _NoPrefetch:
    """Stands in for the prefetch record, which contributes only its accounting keys."""

    def accounting(self) -> dict:
        return {"prefetch_tokens": 0}


def frame(content: str) -> str:
    body = {"choices": [{"index": 0, "delta": {"content": content}}]}
    return "data: " + json.dumps(body) + "\n\n"


def cut_off(*frames: str):
    """A handler whose body delivers `frames` and then times out waiting for the next.

    A read timeout rather than a call-length bound: the latter was retired, so the
    per-chunk timeout is the only thing that ends a stream the endpoint has abandoned,
    and it is the path the partial has to survive.
    """
    async def body():
        for chunk in frames:
            yield chunk.encode()
        raise httpx.ReadTimeout("no further chunk")

    def handler(_request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    return handler


async def test_a_turn_killed_by_the_read_bound_keeps_what_it_decoded():
    """The partial rides out on the exception rather than dying with the accumulator."""
    clock = Clock()
    handler = cut_off(frame("the first half"), frame(" and the second"))
    with pytest.raises(base.BackendUnavailable, match="ReadTimeout") as caught:
        await backend(handler, config=cfg(), clock=clock).complete(request())

    partial = caught.value.partial
    assert partial is not None, "two frames were decoded and the failure reported none"
    assert partial.text == "the first half and the second"


async def test_a_turn_that_never_decoded_carries_no_partial():
    """The negative control for the test above.

    Without it a `partial` hard-coded to an empty response would satisfy the first test
    and mean nothing. Absent and empty are different facts and a caller acts on them
    differently: an empty partial describes a turn that produced nothing, which is the
    shape a stall already reports.
    """
    clock = Clock()
    with pytest.raises(base.BackendUnavailable, match="ReadTimeout") as caught:
        await backend(cut_off(), config=cfg(), clock=clock).complete(request())

    assert caught.value.partial is None


async def test_a_turn_that_never_decoded_was_not_generating():
    """`while_generating` was `True` on the raise whatever had happened.

    It is the field the retry decides on, and its whole purpose is to separate an
    allowance that was spent from one that was not. A turn killed before its first token
    spent queueing and prefill, never decoding, and saying otherwise sends the retry the
    wrong way.
    """
    clock = Clock()
    with pytest.raises(base.BackendUnavailable) as caught:
        await backend(cut_off(), config=cfg(), clock=clock).complete(request())

    assert caught.value.while_generating is False


async def test_a_cancelled_turn_keeps_what_it_decoded():
    """The caller's deadline cancels the call, and the tokens must survive that too.

    This is the path slice 4 is actually about. `_until_deadline` cancels the in-flight
    task, and `CancelledError` is a `BaseException`, so the adapter's `except
    httpx.HTTPError` never sees it and neither would an `except Exception`.
    """
    clock = Clock()
    decoded = asyncio.Event()

    async def body():
        clock.t = 1.0
        yield frame("decoded before the axe").encode()
        decoded.set()
        await asyncio.sleep(3600)

    def handler(_request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    call = backend(handler, config=cfg(), clock=clock).complete(request())
    task = asyncio.ensure_future(call)
    await decoded.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    partial = getattr(caught.value, "partial", None)
    assert partial is not None, "the cancelled turn reported none of what it decoded"
    assert partial.text == "decoded before the axe"


class _DiesHolding:
    """A backend that decodes something, then never finishes.

    It re-raises whatever ends it with the partial attached, which is precisely what the
    adapter now does -- so this double stands in for the adapter without needing a wire.
    It never advances the clock itself; `tick_sleep` below does, so the only thing that
    can end this call is the watchdog deciding the budget is gone.
    """

    def __init__(self, partial):
        self.partial = partial

    async def complete(self, request, *, on_token=None):
        try:
            while True:
                await asyncio.sleep(0)
        except BaseException as e:
            e.partial = self.partial
            raise


def test_the_deadline_carries_the_partial_out_of_the_loop():
    """The seam between the adapter and anything that could report the tokens.

    `_until_deadline` cancels the call and raises a bare `TimeoutError`, discarding the
    cancellation it caught -- so without this the adapter could hold the partial
    perfectly and nothing above would ever see it.

    The fake clock is not a convenience. The watchdog is the one place the deadline logic
    reads real time, so `tick_sleep` is the seam that stops this test spending the budget
    it is measuring.
    """
    clock = FakeClock()

    async def tick_sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    kept = ok_response("everything it managed before the axe")

    with pytest.raises(loop.DispatchTimedOut) as caught:
        asyncio.run(
            loop.complete_with_retry(
                cfg_loop(dispatch_timeout=2, stall_timeout=2, connect_timeout=1),
                _DiesHolding(kept),
                one_shot("hello"),
                deadline=clock() + 2,
                tick_sleep=tick_sleep,
                clock=clock,
            )
        )

    assert caught.value.partial is kept


def test_progress_does_not_drop_the_partial():
    """`with_progress` builds a *copy*, and a copy is where an attribute goes missing.

    The agentic loop replaces the exception with one carrying its turn counters, so every
    timed-out agentic delegation passes through that copy. It is the cheapest possible
    place for this to regress and the hardest to notice, because every test above still
    passes when it does.
    """
    original = loop.DispatchTimedOut(9.0, 10, "while waiting on the backend")
    kept = ok_response("decoded, then abandoned")
    original.partial = kept

    carried = original.with_progress(turns=3, tool_calls=7, last_tool="read_file")

    assert carried.partial is kept
    assert carried.turns == 3


def test_the_result_says_partial_and_still_says_it_failed():
    """The shape the caller actually receives.

    Both keys are asserted because either alone is a different bug: without `partial` the
    caller cannot tell a truncated reply from a finished one, and without `error` a
    deadline reads as a success. The answer is asserted too -- a result carrying the flags
    and an empty answer would satisfy a weaker test and deliver nothing.
    """
    failed = loop.DispatchTimedOut(9.0, 10, "while waiting on the backend")
    failed.partial = ok_response("as far as it got")
    failed.turns, failed.tool_calls = 2, 5

    built = server._partial_result(failed, _NoPrefetch(), None)

    assert built["partial"] is True
    assert built["answer"] == "as far as it got"
    assert "abandoned" in built["error"]
    assert built["turns"] == 2


def test_a_failure_carrying_nothing_still_raises():
    """The negative control for the three above, and the one that keeps this honest.

    `_partial_result` returning a dict for every failure would make every timeout look
    like an answer. `None` is what sends the caller back to `raise`, so a delegation that
    produced no token must still fail exactly as it always did.
    """
    assert server._partial_result(
        loop.DispatchTimedOut(9.0, 10, "while waiting on the backend"),
        _NoPrefetch(),
        None,
    ) is None
