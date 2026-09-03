"""A wedged delegation spent the whole of `dispatch_timeout` producing nothing.

Measured on 2026-09-03, and not hypothetically: eight research delegations were fanned
out at once, and one of them sat for 3600s and died "waiting on the backend" having
completed no turns at all. `turn_timeout` was 1800 and `retry_max_attempts` 3, so that
hour was two full-length wedged attempts back to back. Meanwhile five siblings were
refused by admission after waiting out `admission_wait_timeout`, because the wedged one
was holding a slot it was not using.

Nothing in the loop could tell that case from a delegation that was merely long. The
ceiling bounds total time; it cannot see progress. So raising it -- which ADR-0018's
keepalive had made safe, and which nothing had re-derived -- would have made a stall
strictly more expensive for everyone queued behind it.

`stall_timeout` is the deadline that can see progress: how long a delegation may run
without COMPLETING a turn. This file asserts both directions, because either alone is
worthless here:

  * it fires when no turn completes, and names its own setting rather than the ceiling;
  * it stays silent while turns keep completing past its window -- which is the half that
    proves `last_progress` is actually reset. A deadline measured from the start of the
    delegation would pass the first test and kill healthy work.

The second direction is the one that caught a real bug while this was being written: the
reset first went inside `turn_done`'s `on_turn_done is not None` guard, so a delegation
with no observer attached advanced its progress clock never, and would have been killed
mid-progress. Only a test that completes turns *without* a callback sees that.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import BackendUnavailable, CanonicalResponse
from claude_delegate_local.config import Config
from claude_delegate_local.loop import TextBlock, ToolUseBlock
from claude_delegate_local.registry import ModelEntry

HOST = "http://placeholder.invalid:1"


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def says(text: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),), finish_reason="stop",
        input_tokens=10, output_tokens=5, model="served-id-1",
    )


def wants(name: str = "echo") -> CanonicalResponse:
    return CanonicalResponse(
        content=(ToolUseBlock(id="call-0", name=name, input={}),),
        finish_reason="tool_use", input_tokens=10, output_tokens=5, model="served-id-1",
    )


class Clock:
    """Driven by the backend, never by the wall."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Backend:
    """Answers from a script, charging the clock for each call."""

    def __init__(self, clock: Clock, seconds: float, *replies: CanonicalResponse) -> None:
        self.clock = clock
        self.seconds = seconds
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        self.clock.now += self.seconds
        if not self.replies:
            raise AssertionError("the loop called the backend past its script")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


class NeverFinishes(Backend):
    """Alive, answering, and never reaching a final answer: a tool call every time.

    Worth being precise about what this is, because the first draft of this file used it
    as the stall case and was wrong. Every turn here *completes* -- the model replies, the
    tool runs, the turn ends -- so this delegation is making progress and is merely long.
    It is the case a stall deadline must NOT kill, and it belongs in this file as the
    control rather than as the subject.
    """

    def __init__(self, clock: Clock, seconds: float) -> None:
        super().__init__(clock, seconds)

    async def complete(self, request):
        self.calls += 1
        self.clock.now += self.seconds
        return wants()


class Failing(Backend):
    """Reachable, and never getting anywhere: every call a retryable failure."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        super().__init__(clock, seconds)

    async def complete(self, request):
        self.calls += 1
        self.clock.now += self.seconds
        raise BackendUnavailable("dropped")


async def _no_sleep(seconds: float) -> None:
    """The backoff costs the clock nothing here; the calls themselves move it."""


def run(backend, clock, *, config, turns=50, **over):
    return asyncio.run(
        loop.run_agentic_loop(
            config,
            ModelEntry(key="flash", base_url=HOST, served_model_id="served-id-1"),
            backend,
            loop.Delegation("do the thing"),
            allowed=frozenset({"echo"}),
            max_turns=turns,
            clock=clock,
            **over,
        )
    )


@pytest.fixture
def registered():
    """One tool that costs nothing and always succeeds, so only the clock decides.

    `REGISTRY` is one dict object shared by `tools.py` and `loop.py`'s import of it, so
    mutating it is what both sites actually read -- the same approach `test_agentic_loop`
    takes, and removed again afterwards for the same reason.
    """
    def echo(cfg_, args):
        return "echoed"

    added = {
        "echo": tools.RegisteredTool(
            spec=tools.ToolSpec(
                name="echo", description="echo", input_schema={"type": "object"},
            ),
            handler=echo,
        ),
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def test_the_stall_deadline_fires_when_no_turn_ever_completes(registered):
    """One wedged attempt is permitted; a second with nothing to show is not.

    A stall is a delegation that cannot finish a turn, so the backend here never returns
    one: every call is a retryable failure that charges the clock first. That is the
    2026-09-03 shape -- `turn_timeout` elapsing twice inside one `dispatch_timeout`.
    """
    clock = Clock()
    backend = Failing(clock, 700.0)
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=14400,
                 retry_max_attempts=9, retry_base_delay=0.01, retry_max_delay=0.01)

    with pytest.raises(loop.DispatchTimedOut) as e:
        run(backend, clock, config=config, sleep=_no_sleep)

    assert e.value.setting == "DELEGATE_STALL_TIMEOUT", (
        "a stall reported against the ceiling sends an operator to raise a setting that "
        "had no part in it -- and that ceiling is now four hours"
    )
    assert e.value.limit == 900
    assert backend.calls < 9, "it must stop on the deadline, not on the attempt counter"
    assert clock.now - 1000.0 < 14400, "it must stop on progress, not on the ceiling"
    assert "raising this would only lengthen the wait" in str(e.value), (
        "the remedy for a ceiling is wrong for a stall"
    )


def test_a_merely_long_delegation_still_dies_on_the_ceiling_and_says_so(registered):
    """The distinction, from the other side.

    This delegation completes a turn every 700s forever, so the no-progress deadline never
    fires and should not -- it is working. What stops it is the absolute ceiling, and the
    failure has to name that, because here "raise the setting or shorten the task" is
    genuinely the right advice.

    Without this case the stall deadline could have been written to fire on elapsed time
    and both of the tests above would still pass.
    """
    clock = Clock()
    backend = NeverFinishes(clock, seconds=700)
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=3500)

    with pytest.raises(loop.DispatchTimedOut) as e:
        run(backend, clock, config=config)

    assert e.value.setting == "DELEGATE_DISPATCH_TIMEOUT"
    assert e.value.limit == 3500
    assert backend.calls > 1, "it must have completed turns rather than wedging"


def test_the_stall_deadline_stays_silent_while_turns_keep_completing(registered):
    """The half that proves `last_progress` is reset rather than measured from the start.

    Four turns, each costing 400s, against a 900s no-progress window: the delegation runs
    1600s in total -- well past the window -- and must not be touched, because it completed
    a turn every 400s. A deadline measured from entry would kill this on turn three.

    No `on_turn_done` is passed, deliberately. The reset first lived inside the callback
    guard, so a delegation nobody was observing never advanced its progress clock; this
    argument is the whole reason that bug is not still here.
    """
    clock = Clock()
    backend = Backend(clock, 400.0, wants(), wants(), wants(), says("done"))
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=14400)

    result = run(backend, clock, config=config)

    assert result.response.text == "done"
    assert result.turns == 4
    assert clock.now - 1000.0 > 900, "the run must outlast the window for this to prove it"


def test_a_one_shot_is_bounded_by_the_tighter_of_the_two():
    """A one-shot never completes a turn, so the no-progress deadline counts from entry.

    That is what stops the raised ceiling becoming this path's only bound: it has no turns
    to report, so `min(dispatch_timeout, stall_timeout)` is the honest limit, and it falls
    out of the same mechanism rather than a special case.
    """
    clock = Clock()
    # Retryable failures rather than a slow success. A call that answers is not a stall
    # however many seconds it charged the clock, which is the first thing running this
    # test said -- the deadline is consulted before an attempt and before a retry sleep,
    # so provoking it needs the loop to come back round with nothing.
    backend = Failing(clock, 1000.0)
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=14400,
                 retry_max_attempts=9, retry_base_delay=0.01, retry_max_delay=0.01)

    with pytest.raises(loop.DispatchTimedOut) as e:
        asyncio.run(
            loop.run_one_shot(
                config,
                ModelEntry(key="flash", base_url=HOST, served_model_id="served-id-1"),
                backend,
                loop.Delegation("do the thing"),
                sleep=_no_sleep,
                clock=clock,
            )
        )

    assert e.value.setting == "DELEGATE_STALL_TIMEOUT", (
        "the one-shot has no turns, so the ceiling would otherwise be its only bound -- "
        "and that ceiling is now four hours"
    )
    assert backend.calls < 9, "it must stop on the deadline, not on the attempt counter"
    assert clock.now - 1000.0 < 14400


def test_a_stall_deadline_outside_the_nesting_is_refused_at_load():
    """A deadline below `turn_timeout` cuts short a call that was merely slow, and one
    above the ceiling can never fire. Both are configuration errors rather than surprises
    at runtime, and the message says which bound was crossed."""
    with pytest.raises(Exception, match="DELEGATE_STALL_TIMEOUT"):
        cfg(turn_timeout=600, stall_timeout=500, dispatch_timeout=14400)
    with pytest.raises(Exception, match="DELEGATE_STALL_TIMEOUT"):
        cfg(turn_timeout=600, stall_timeout=20000, dispatch_timeout=14400)
    # Equal is legal at both ends: `turn_timeout == dispatch_timeout` is a configuration
    # this project permits, and a strict lower bound would leave it no legal value at all.
    cfg(turn_timeout=600, stall_timeout=600, dispatch_timeout=600)
