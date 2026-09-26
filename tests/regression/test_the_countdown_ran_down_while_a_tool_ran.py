"""While a turn's tools run, the heartbeat's countdown reported a deadline that cannot fire.

`_keepalive` was given `max(min(stall_left(), deadline - clock()), 0.0)`, and the stall
clock is only ever read from inside the backend call and is reset only after the tools
finish -- so during the `await asyncio.to_thread(_run_calls, ...)` the stall deadline is
simply not enforced. A tool that runs longer than the stall budget therefore shows a
watcher a countdown that falls to 0 while the delegation is nowhere near ending: only
`dispatch_timeout` can end the run then, and the countdown should name that figure
(`deadline - clock()`), well above zero and above the stall budget.

The test drives the tool on the real clock (so the heartbeat, which sleeps on the real
clock, fires while it runs) and on the fake clock (so `stall_left` is genuinely past the
budget), and asserts that the beat recorded during the tool reports the delegation
deadline, not the stall one.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from test_agentic_loop import ScriptedTurns, cfg, entry, says, wants

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import ToolSpec

START = 1000.0  # the fake clock's initial reading
STALL = 100
DISPATCH = 10000


def _tick_sleep_on(now):
    """The watchdog's wait, spent on the fake clock.

    Without this seam `_until_deadline` waits on the wall while the doubles move the fake
    clock instantly, so the deadline never gets a chance to fire and *every* case passes.
    Copied from `test_stall_excludes_tool_time.py`; the tool is what moves the clock here,
    but the watchdog still needs to sleep somewhere that does not drag the test out.
    """
    async def tick_sleep(seconds: float) -> None:
        now[0] += seconds
        await asyncio.sleep(0)

    return tick_sleep


def test_the_countdown_during_a_tool_names_the_delegation_deadline():
    """The bug. `ends_in` while the tool ran read 0, not the ~9700s the delegation had left.

    A tool that spends real time (so the heartbeat, on the real clock, fires while it runs)
    and that advances the fake clock past the stall budget (so `stall_left` is genuinely
    exhausted) must not be reported against the stall deadline -- that deadline is not even
    checked during a tool, only the delegation deadline can end the run then.
    """
    now = [START]
    in_tool = [False]
    seen: list[tuple[float, float, bool]] = []

    def slow(cfg_, args):
        now[0] += 300.0  # past the stall budget, on the fake clock
        in_tool[0] = True
        try:
            time.sleep(2.5)  # real time, so the heartbeat's real-clock timer fires during it
        finally:
            in_tool[0] = False
        return "took a while"

    tools.REGISTRY["slow"] = tools.RegisteredTool(
        spec=ToolSpec(name="slow", description="slow", input_schema={"type": "object"}),
        handler=slow,
        cacheable=False,
    )

    async def on_alive(elapsed: float, of: int, ends_in: float,  # noqa: PLR0913, PLR0917 -- the heartbeat's positional arity, fixed by _keepalive
                       chunks: int = 0, reasoning_chunks: int = 0,
                       since: float | None = None) -> None:
        seen.append((ends_in, now[0], in_tool[0]))

    try:
        asyncio.run(
            loop.run_agentic_loop(
                cfg(keepalive_interval=1, stall_timeout=STALL, dispatch_timeout=DISPATCH),
                entry(),
                ScriptedTurns(wants(("slow", {})), says("done")),
                loop.Delegation("do the thing"),
                allowed=frozenset({"slow"}),
                max_turns=5,
                clock=lambda: now[0],
                tick_sleep=_tick_sleep_on(now),
                on_alive=on_alive,
            )
        )
    finally:
        tools.REGISTRY.pop("slow", None)

    during = [b for b in seen if b[2]]
    assert during, "the heartbeat never beat while the tool was running"
    ends_in, at_clock, _ = max(during, key=lambda b: b[0])
    expected = START + DISPATCH - at_clock  # deadline - clock(), the delegation ceiling
    assert ends_in == pytest.approx(expected, abs=5.0), (
        f"while the tool ran the countdown read {ends_in}s at fake clock {at_clock}; the "
        f"delegation deadline was {expected}s away. Only dispatch_timeout can end the run "
        f"during a tool, so this reports the stall deadline ({STALL}s) that cannot fire then."
    )
    assert ends_in > STALL, (
        f"the countdown during the tool read {ends_in}s, below the stall budget of {STALL}s "
        f"that is not even enforced while the tool runs"
    )
