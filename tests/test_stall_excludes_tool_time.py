"""A tool that runs longer than the stall budget does not kill the delegation.

`stall_left` is frozen while a tool runs -- nothing resets `last_progress` between the
last token of a turn and the tool results coming back -- so the countdown a watcher sees
falls straight through the tool window. That much is real, and it is what U.19 recorded.

What does *not* follow, and is asserted here, is that the delegation dies of it. The stall
clock is only ever read from inside `complete_with_retry`, which is not running while the
tools are, and `turn_done` resets `last_progress` after `_run_calls` returns rather than
before. So the next turn starts with a whole budget no matter how long the tools took.

This matters because lowering `stall_timeout` is only safe if that is true. The test
exists to keep it true.
"""

from __future__ import annotations

import asyncio

import pytest
from test_agentic_loop import ScriptedTurns, cfg, entry, says, wants

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import ToolSpec


@pytest.fixture
def slow_tool():
    """A registered tool that spends far more than the stall budget.

    `REGISTRY` is the one dict both `tools.py` and `loop.py` read, so mutating it here is
    what the loop actually sees.
    """
    now = [1000.0]
    spent = [0.0]

    def slow(cfg_, args):
        now[0] += 300.0
        spent[0] += 300.0
        return "took a while"

    tools.REGISTRY["slow"] = tools.RegisteredTool(
        spec=ToolSpec(name="slow", description="slow", input_schema={"type": "object"}),
        handler=slow,
        cacheable=False,
    )
    try:
        yield now, spent
    finally:
        tools.REGISTRY.pop("slow", None)


def _tick_sleep_on(now):
    """The watchdog's wait, spent on the fake clock.

    Without this seam `_until_deadline` waits on the wall while the doubles move the fake
    clock instantly, so the deadline never gets a chance to fire and *every* case passes
    -- including the ones that must not. It is the difference between a control and a
    decoration.
    """
    async def tick_sleep(seconds: float) -> None:
        now[0] += seconds
        await asyncio.sleep(0)

    return tick_sleep


def test_a_tool_outlasting_the_stall_budget_does_not_kill_the_turn(slow_tool):
    """300s of tool against a 30s stall budget, and the delegation still answers."""
    now, spent = slow_tool
    backend = ScriptedTurns(wants(("slow", {})), says("done"))

    result = asyncio.run(
        loop.run_agentic_loop(
            cfg(stall_timeout=30, dispatch_timeout=3600),
            entry(),
            backend,
            loop.Delegation("do the thing"),
            allowed=frozenset({"slow"}),
            max_turns=5,
            clock=lambda: now[0],
            tick_sleep=_tick_sleep_on(now),
        )
    )

    assert spent[0] == 300.0, "the tool did not actually spend the clock"
    assert result.response.text == "done"


def test_a_silent_backend_still_dies_on_the_same_budget(slow_tool):
    """The negative control. Without it the test above would pass against a stall clock
    that had been disabled entirely, which is the change ADR-0047 refused."""
    now, _spent = slow_tool

    class Silent(ScriptedTurns):
        async def complete(self, request, *, on_token=None):
            # Spends the budget inside the backend call, where the watchdog is watching,
            # and emits nothing. Yields so the watchdog gets to read the moved clock.
            for _ in range(60):
                now[0] += 5.0
                await asyncio.sleep(0)
            return await super().complete(request)

    backend = Silent(wants(("slow", {})), says("done"))

    with pytest.raises(loop.DispatchTimedOut) as caught:
        asyncio.run(
            loop.run_agentic_loop(
                cfg(stall_timeout=30, dispatch_timeout=3600),
                entry(),
                backend,
                loop.Delegation("do the thing"),
                allowed=frozenset({"slow"}),
                max_turns=5,
                clock=lambda: now[0],
                tick_sleep=_tick_sleep_on(now),
            )
        )

    assert caught.value.setting == "DELEGATE_STALL_TIMEOUT", caught.value.setting
