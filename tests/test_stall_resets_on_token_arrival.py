"""The no-progress deadline counts token arrival as progress.

ADR-0047 chose turn completion as the stall signal because every alternative available at
the time was fake: the per-turn notification fires at the *top* of a turn, and the keepalive
is a timer, so both reset the clock on the very turn that wedged. The cost was that a model
reasoning productively inside one long turn is indistinguishable from a wedged one, and the
deadline kills both -- one pass died in its thirtieth turn, having completed twenty-nine.

Streaming supplies the signal ADR-0047 lacked. A call producing tokens is making progress by
any honest reading, and a call producing nothing still dies. That second half is not a
nicety: it is what stops this being the `alive` heartbeat all over again, reporting liveness
on a timer regardless of whether anything is happening (ADR-0072).
"""

from __future__ import annotations

import asyncio

import pytest
from test_loop import FakeClock, ScriptedBackend, cfg, ok_response, one_shot

from claude_delegate_local import loop


class TricklingBackend(ScriptedBackend):
    """A backend that spends clock time, emitting a token every `every` seconds.

    The two knobs are what separate the cases the deadline must tell apart: `emits` false
    is a call burning the same wall time in silence, which must still be killed.
    """

    def __init__(self, script, clock: FakeClock, *, seconds: float,
                 every: float = 1.0, emits: bool = True) -> None:
        super().__init__(script)
        self.clock = clock
        self.seconds = seconds
        self.every = every
        self.emits = emits

    async def complete(self, request, *, on_token=None):
        spent = 0.0
        while spent < self.seconds:
            step = min(self.every, self.seconds - spent)
            self.clock.advance(step)
            spent += step
            if self.emits and on_token is not None:
                on_token()
            # Yield, so the deadline watchdog gets to read the clock this call is moving.
            # Without it the loop never suspends, the watchdog never runs, and both cases
            # below pass for the wrong reason.
            await asyncio.sleep(0)
        return await super().complete(request)


def _run(backend, clock, *, stall_timeout: int):
    """One attempt, bounded by a stall deadline that starts counting now."""
    last_progress = clock()

    def stall_left() -> float:
        return stall_timeout - (clock() - last_progress)

    def progressed() -> None:
        nonlocal last_progress
        last_progress = clock()

    async def tick_sleep(seconds: float) -> None:
        """The watchdog's wait, spent on the fake clock instead of the wall.

        The one place the deadline logic reads real time, so without this seam the test
        would have to spend thirty real seconds to find out whether thirty fake ones are
        enforced -- and the backend double, which moves the fake clock instantly, would
        finish long before the first real tick elapsed.
        """
        clock.advance(seconds)
        await asyncio.sleep(0)

    return asyncio.run(
        loop.complete_with_retry(
            cfg(dispatch_timeout=3600, turn_timeout=stall_timeout,
                stall_timeout=stall_timeout),
            backend,
            one_shot("hello"),
            deadline=clock() + 3600,
            stall_left=stall_left,
            on_token=progressed,
            tick_sleep=tick_sleep,
            clock=clock,
        )
    )


def test_a_call_still_producing_tokens_is_not_killed():
    """Ten times the stall budget, survived, because it never stopped producing."""
    clock = FakeClock()
    backend = TricklingBackend(
        [ok_response("finished eventually")], clock, seconds=300, every=5
    )

    response, attempts, _answered = _run(backend, clock, stall_timeout=30)

    assert response.text == "finished eventually"
    assert attempts == 1


def test_a_call_producing_nothing_still_dies():
    """The negative control, and the half that makes the check capable of failing.

    Same backend, same clock, same wall time -- only the tokens are gone. Without this a
    change that reset the deadline unconditionally would pass the test above, and that
    change is exactly the one ADR-0047 refused: it would have called the 2026-09-04 stalls
    healthy, every one of which had an endpoint reporting itself idle throughout.
    """
    clock = FakeClock()
    backend = TricklingBackend(
        [ok_response("never reached")], clock, seconds=300, every=5, emits=False
    )

    with pytest.raises(loop.DispatchTimedOut) as caught:
        _run(backend, clock, stall_timeout=30)

    assert caught.value.setting == "DELEGATE_STALL_TIMEOUT"
