"""The decode rate divided one attempt's tokens by every attempt's seconds.

`backend_seconds` is measured around `dispatch_with_recovery`, which runs up to three
recovery stages with a transport retry inside each. `output_tokens` comes only from the
attempt that answered — ADR-0014 requires that, so a turn is charged for the answer it
got. Dividing the second by the first is arithmetic over two different events.

Measured across 46 recorded turns: single-attempt turns averaged 27.2 tok/s and
multi-attempt turns 11.1, with the slowest at 0.9. Six of the ten slowest turns had made
two attempts. `DecodeRate` then seeds the *next* delegation's first turn from that average,
so a number describing nothing was the one deciding whether a budget was payable.

The fix measures the answering attempt rather than discarding the turn. The repo already
states the principle it violates, in `loop.py`: a throughput number quietly measuring the
wrong interval is worse than none, because it gets believed.

What this does NOT fix is prefill. The answering attempt still contains its own prompt
processing, so a turn with a large prompt and a short answer still reads slow — 442 tokens
against 54,052 of input took 50.2s, of which roughly 30 is prefill. Only streaming can
separate those, and `MIN_TOKENS` guards the wrong end of it. That is left measured and
stated rather than silently modelled.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest
from test_loop import FakeClock, a_reply, cfg, one_shot

from claude_delegate_local import loop
from claude_delegate_local.backends.base import BackendUnavailable


class SlowThenFast:
    """Fails once after a long interval, then answers after a short one.

    The clock is advanced by the backend itself, so the two intervals are exact rather
    than raced: without that the test would be asserting against scheduler noise.
    """

    def __init__(self, clock: FakeClock, first: float, second: float) -> None:
        self.clock, self.first, self.second, self.calls = clock, first, second, 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            self.clock.advance(self.first)
            raise BackendUnavailable("ConnectError posting to /v1/chat/completions")
        self.clock.advance(self.second)
        return a_reply()


def run(clock: FakeClock, backend, **over):
    config = cfg(retry_base_delay=0, retry_max_delay=0, **over)

    async def sleep(_seconds: float) -> None:
        return None

    return asyncio.run(loop.complete_with_retry(
        config, backend, one_shot("hello"),
        sleep=sleep, deadline=None, stall_left=lambda: 10_000.0, clock=clock,
    ))


def test_the_answering_attempt_is_what_is_timed():
    """The bug. 40s of failure plus 10s of answer was reported as a 50s answer."""
    clock = FakeClock()
    _, attempts, answered = run(clock, SlowThenFast(clock, first=40.0, second=10.0))
    assert attempts == 2
    assert answered == pytest.approx(10.0), (
        f"timed {answered}s, which is the whole turn rather than the answering attempt"
    )


def test_a_single_attempt_is_timed_the_same_way():
    """The other direction: the fix must not change the case that was already right."""
    clock = FakeClock()

    class Answers:
        async def complete(self, request):
            clock.advance(7.0)
            return a_reply()

    _, attempts, answered = run(clock, Answers())
    assert attempts == 1
    assert answered == pytest.approx(7.0)


def test_the_retry_wait_is_not_charged_to_the_answer():
    """The backoff between attempts is not generation and must not be in the divisor.

    It is the same error as counting the failed attempt, one layer down, and a fix that
    timed from the retry's start rather than the attempt's would reintroduce it.
    """
    clock = FakeClock()
    backend = SlowThenFast(clock, first=5.0, second=3.0)

    async def sleep(seconds: float) -> None:
        clock.advance(60.0)  # a long backoff, charged to nobody

    _, _, answered = asyncio.run(loop.complete_with_retry(
        cfg(retry_base_delay=60, retry_max_delay=60), backend, one_shot("hello"),
        sleep=sleep, deadline=None, stall_left=lambda: 10_000.0, clock=clock,
    ))
    assert answered == pytest.approx(3.0), (
        f"timed {answered}s, so the backoff was counted as generation"
    )
