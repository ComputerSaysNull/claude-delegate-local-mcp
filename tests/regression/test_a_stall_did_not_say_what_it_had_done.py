"""A stalled delegation named the deadline and nothing about its own progress.

Twice on 2026-09-04 a delegation died on `stall_timeout` while the endpoint was perfectly
healthy -- the task was simply too large to finish a turn. The failure said which setting
fired and stopped there, so at the call site it was indistinguishable from an endpoint that
never answered, and the honest response to *that* is to stop using the server. Both times
that was the wrong call.

The counters existed and were unreachable. `_Watch` is a local of `run_agentic_loop` and is
discarded as the exception propagates; `AgenticDispatch`, which carries `turns` and
`tool_calls`, is built only when the loop finishes. So the fix is one handler on the way out,
and these are the three things worth asserting about it:

  * a stall that had achieved something says so, with real counts;
  * a stall that had achieved *nothing* says that too, in the same shape -- which is what
    actually separates the two cases, rather than a differently worded message;
  * a failure with no counters available renders exactly the text it always did, because
    absent and zero are different facts. The one-shot path completes no turns by
    construction, so a hardcoded zero there would describe the one path that cannot stall
    as though it had stalled.

Verified to fail without the fix: with the `except DispatchTimedOut` block removed from
`run_agentic_loop`, the two loop tests fail on the missing progress clause while the
message-level tests still pass -- which is why both levels are here.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import BackendUnavailable, CanonicalResponse
from claude_delegate_local.config import Config
from claude_delegate_local.loop import DispatchTimedOut, ToolUseBlock
from claude_delegate_local.registry import ModelEntry

HOST = "http://placeholder.invalid:1"


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


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


class WedgesAfter:
    """Completes `good` turns, then never gets anywhere again.

    The shape of 2026-09-04: real work happened, and then the delegation stopped being able
    to finish a turn. A backend that fails from the first call cannot distinguish the fix
    from a hardcoded zero, which is why `good` is a parameter and both values are tested.
    """

    def __init__(self, clock: Clock, seconds: float, good: int) -> None:
        self.clock = clock
        self.seconds = seconds
        self.good = good
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        self.clock.now += self.seconds
        if self.calls <= self.good:
            return wants()
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
    mutating it is what both sites actually read -- and removed again afterwards.
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


# --------------------------------------------------------------- the message on its own


def test_a_failure_with_no_counters_renders_the_text_it_always_did():
    """The negative half, and the reason `_progress` returns "" rather than zeros.

    Asserted as an absence, because this is the case that must not change: a one-shot has no
    turn loop, so nothing can supply counters, and inventing "0 turns completed" there would
    read as a stall on the one path that cannot stall.
    """
    e = DispatchTimedOut(2100.0, 2100, "with no turn completed",
                         setting="DELEGATE_STALL_TIMEOUT", remedy="Check the endpoint.")

    assert "Progress at that point" not in str(e), (
        "an absent counter must not be reported as a zero -- they are different facts"
    )
    assert e.turns is None and e.tool_calls is None and e.last_tool is None
    assert str(e) == (
        "Delegation abandoned after 2100.0s, past the DELEGATE_STALL_TIMEOUT of 2100s, "
        "with no turn completed. Check the endpoint."
    )


def test_the_stage_reads_as_a_sentence():
    """The template used to supply a literal "while", so the stall read "while with no...".

    Three stages wanted the preposition and one did not, and the one that did not is the
    message an operator stares at longest.
    """
    stall = DispatchTimedOut(2100.0, 2100, "with no turn completed",
                             setting="DELEGATE_STALL_TIMEOUT")
    ceiling = DispatchTimedOut(14400.0, 14400, "while waiting on the backend")

    assert "while with" not in str(stall)
    assert "2100s, with no turn completed." in str(stall)
    assert "14400s, while waiting on the backend." in str(ceiling)


def test_with_progress_keeps_every_other_field():
    """A copy, not a mutation -- so `str(e)` cannot disagree with the fields beside it."""
    original = DispatchTimedOut(2100.0, 2100, "with no turn completed",
                                setting="DELEGATE_STALL_TIMEOUT", remedy="Check it.")
    copy = original.with_progress(turns=3, tool_calls=6, last_tool="read_file")

    assert (copy.elapsed, copy.limit, copy.stage) == (2100.0, 2100, "with no turn completed")
    assert copy.setting == "DELEGATE_STALL_TIMEOUT"
    assert copy.remedy == "Check it."
    assert "Progress at that point: 3 turns completed, 6 tool calls, last tool read_file." \
        in str(copy)
    assert "Progress at that point" not in str(original), "the original must be untouched"


def test_one_turn_and_one_call_are_not_pluralised():
    e = DispatchTimedOut(900.0, 900, "with no turn completed").with_progress(
        turns=1, tool_calls=1, last_tool=None)
    assert "1 turn completed, 1 tool call." in str(e)
    assert "last tool" not in str(e), "an unknown last tool is omitted, not named None"


# ------------------------------------------------------------------ through the real loop


def test_a_stall_that_had_achieved_something_says_so(registered):
    """The 2026-09-04 case: a turn completed, then nothing could finish.

    One good turn resets `last_progress`, so the stall window runs from there. What the
    message must carry is that a turn *did* complete and a tool *was* called -- the evidence
    that the endpoint is answering and the task is the problem.
    """
    clock = Clock()
    backend = WedgesAfter(clock, 700.0, good=1)
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=14400,
                 retry_max_attempts=9, retry_base_delay=0.01, retry_max_delay=0.01)

    with pytest.raises(DispatchTimedOut) as e:
        run(backend, clock, config=config, sleep=_no_sleep)

    assert e.value.setting == "DELEGATE_STALL_TIMEOUT"
    assert e.value.turns == 1, "one turn completed before it wedged"
    assert e.value.tool_calls == 1
    assert e.value.last_tool == "echo"
    assert "1 turn completed, 1 tool call, last tool echo." in str(e.value)


def test_a_stall_that_had_achieved_nothing_says_that_too(registered):
    """The case that looks like a dead endpoint, and now reports itself as one.

    The distinction is carried by the numbers, not by different wording: zero turns and zero
    calls is what an endpoint that never answered looks like, and a caller can act on that.
    Reporting nothing at all -- which is what happened before -- left both cases identical.
    """
    clock = Clock()
    backend = WedgesAfter(clock, 700.0, good=0)
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=14400,
                 retry_max_attempts=9, retry_base_delay=0.01, retry_max_delay=0.01)

    with pytest.raises(DispatchTimedOut) as e:
        run(backend, clock, config=config, sleep=_no_sleep)

    assert e.value.turns == 0
    assert e.value.tool_calls == 0
    assert e.value.last_tool is None
    assert "0 turns completed, 0 tool calls." in str(e.value)


def test_a_stall_never_borrows_the_unreachable_vocabulary(registered):
    """`backend_unreachable` is a different diagnosis and must stay one.

    The stall path deliberately does not route through `_refuse`, because that names an
    endpoint as the thing at fault. This asserts the enriched message did not quietly
    reintroduce the confusion it exists to remove.
    """
    clock = Clock()
    backend = WedgesAfter(clock, 700.0, good=1)
    config = cfg(turn_timeout=600, stall_timeout=900, dispatch_timeout=14400,
                 retry_max_attempts=9, retry_base_delay=0.01, retry_max_delay=0.01)

    with pytest.raises(DispatchTimedOut) as e:
        run(backend, clock, config=config, sleep=_no_sleep)

    assert "backend_unreachable" not in str(e.value)
    assert "Check the endpoint with backend_status" in str(e.value), (
        "it should still suggest the probe -- suggesting it is not the same as claiming it"
    )
