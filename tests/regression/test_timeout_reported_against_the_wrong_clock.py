"""`DispatchTimedOut` reported one turn's elapsed time against the whole delegation's limit.

Found on 2026-09-02, by running delegations rather than by reading the code. A saturated
cluster produced this, verbatim:

    Delegation abandoned after 1372.8s, past the DELEGATE_DISPATCH_TIMEOUT of 3600s

1372.8s is not past 3600s. `complete_with_retry` set `started = clock()` on entry and
reported `clock() - started`, while `deadline` is taken once for the whole delegation and
handed down -- and `run_agentic_loop` calls `complete_with_retry` fresh on every turn. So
the elapsed time was scoped to the current turn and the limit beside it to the whole
delegation. The exception's own docstring says it means "the whole delegation outlived
`dispatch_timeout`", so the delegation-scoped number was always the intended one.

The message is the whole value of this exception: ADR-0007's argument for server-captured
truth is what makes it trustworthy, and its remedy ("raise that setting") is advice the
printed number did not support.

**Why the suite missed it.** Every existing test passes `deadline=clock() + dispatch_timeout`
at the moment it calls `complete_with_retry`, so the deadline's origin and the function's
own start coincide and the two measurements agree by construction. Nothing set a deadline
*before* entering -- which is precisely what the turn loop does. The tests here spend part
of the budget first.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import BackendUnavailable
from claude_delegate_local.config import Config

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    # The deadlines have to nest, and these tests set absurdly small ceilings on purpose so
    # a fake clock is cheap. Follow the ceiling down unless the test names this itself, so
    # shrinking `dispatch_timeout` does not silently become a test of the stall deadline.
    if "dispatch_timeout" in kw and "stall_timeout" not in kw:
        kw["stall_timeout"] = kw["dispatch_timeout"]
    return Config(**kw)  # type: ignore[arg-type]


def request():
    return loop.build_one_shot_request(
        delegation=loop.Delegation("a task"), effort="low", max_tokens=100,
        temperature=1.0,
    )


class Clock:
    """A monotonic clock the test drives, so no test spends the deadline it asserts on."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Dropping:
    """Costs the clock, then fails retryably -- the shape that drives a deadline."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.calls = 0

    async def complete(self, _request):
        self.calls += 1
        self.clock.advance(self.seconds)
        raise BackendUnavailable("dropped")


class Hanging:
    """Raises the timeout an adapter's own client budget would raise."""

    async def complete(self, _request):
        raise TimeoutError


def test_elapsed_is_not_reported_below_the_limit_it_claims_to_exceed():
    """The bug at its starkest: the budget is already gone when the call is entered.

    Against the old code `started` is the moment of entry, so nothing has elapsed by its
    reckoning and the refusal reads "abandoned after 0.0s, past the ... of 100s".
    """
    clock = Clock()
    deadline = clock() + 100      # taken for the whole delegation
    clock.advance(150)            # ...and overspent by earlier turns

    with pytest.raises(loop.DispatchTimedOut) as excinfo:
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=100, turn_timeout=100),
                Dropping(clock, seconds=1), request(),
                sleep=_no_sleep, jitter=lambda lo, hi: hi,
                deadline=deadline, clock=clock,
            )
        )

    e = excinfo.value
    assert e.limit == 100
    assert e.elapsed >= e.limit, (
        f"reported {e.elapsed}s as past a limit of {e.limit}s, which it is not"
    )
    assert e.elapsed == pytest.approx(150)
    assert "past the DELEGATE_DISPATCH_TIMEOUT of 100s" in str(e)


def test_a_deadline_reached_while_waiting_to_retry_counts_the_whole_budget():
    """The other raise site, reached through the backoff guard rather than the top check."""
    clock = Clock()
    deadline = clock() + 100
    clock.advance(95)             # 95s of the budget already spent by earlier turns

    with pytest.raises(loop.DispatchTimedOut) as excinfo:
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=100, turn_timeout=100, retry_max_attempts=5,
                    retry_base_delay=30.0, retry_max_delay=30.0),
                Dropping(clock, seconds=10), request(),
                sleep=_no_sleep, jitter=lambda lo, hi: hi,
                deadline=deadline, clock=clock,
            )
        )

    e = excinfo.value
    assert e.stage == "while waiting to retry"
    assert e.elapsed >= e.limit, (
        f"reported {e.elapsed}s as past a limit of {e.limit}s, which it is not"
    )


def test_a_delegation_inside_its_budget_is_still_untouched():
    """The other direction. Reporting a deadline that has not been reached would be worse
    than the bug, so the fix must not make one out of arithmetic."""
    clock = Clock()
    deadline = clock() + 1000
    clock.advance(10)

    async def go():
        return await loop.complete_with_retry(
            cfg(dispatch_timeout=1000, turn_timeout=1000, retry_max_attempts=1),
            Dropping(clock, seconds=1), request(),
            sleep=_no_sleep, jitter=lambda lo, hi: hi,
            deadline=deadline, clock=clock,
        )

    with pytest.raises(BackendUnavailable):
        asyncio.run(go())


def test_a_turn_timeout_does_not_blame_the_delegation_deadline():
    """With no deadline in play, only the adapter's client budget can have expired.

    The empty-answer stages pass `deadline=None` once they have spent the budget
    themselves, and a `TimeoutError` arriving there used to be reported as
    DELEGATE_DISPATCH_TIMEOUT -- sending an operator to raise a setting that had no part
    in it.
    """
    clock = Clock()
    with pytest.raises(loop.DispatchTimedOut) as excinfo:
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=3600, turn_timeout=42),
                Hanging(), request(),
                sleep=_no_sleep, jitter=lambda lo, hi: hi,
                deadline=None, clock=clock,
            )
        )

    e = excinfo.value
    assert e.setting == "DELEGATE_TURN_TIMEOUT"
    assert e.limit == 42
    assert "DELEGATE_DISPATCH_TIMEOUT" not in str(e)


async def _no_sleep(_seconds: float) -> None:
    """Never waits. Any test that needed a real wait would be spending its own deadline."""
    return None
