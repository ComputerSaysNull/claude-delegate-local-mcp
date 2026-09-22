"""The heartbeat reported progress against the one deadline that was not going to fire.

`_keepalive` sent `cfg.dispatch_timeout` as the figure a caller measures elapsed against.
That is the whole-delegation ceiling — 14400s by default — while what actually kills a
turn is the no-progress deadline, far below it. So every one of the nine delegations
abandoned at 2100s spent its last half hour reporting "60s of 14400s": four tenths of one
percent elapsed, while minutes from death.

The fix adds the number rather than redefining the old one. `of_seconds` still means the
delegation ceiling, which is true and which old transcripts already carry; `ends_in_seconds`
is the new and actionable one.

It is deliberately *not* `budget_seconds`, though the shapes look close. That function
sizes one reply against the delegation clock alone, because a stream that is producing is
not silent and must not be charged the stall clock; a countdown has the opposite job, and
must name whichever deadline will actually end the run. Sizing a reply and counting down a
delegation are two questions, and only the second is what a watcher is asking.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

from test_loop import SleepingBackend, a_reply, cfg, entry

from claude_delegate_local import loop


def beats(**over) -> list[tuple[float, int, float]]:
    """Run a one-shot slow enough to beat, and collect every heartbeat it sent."""
    seen: list[tuple[float, int, float]] = []

    async def on_alive(elapsed: float, of: int, ends_in: float,
                       chunks: int = 0, since: float | None = None) -> None:
        seen.append((elapsed, of, ends_in))

    async def go() -> None:
        await loop.run_one_shot(
            cfg(keepalive_interval=1, **over), entry(),
            SleepingBackend(a_reply(), 2.5), loop.Delegation("hello"),
            on_alive=on_alive,
        )

    asyncio.run(go())
    return seen


def test_the_heartbeat_says_how_long_is_left_not_only_how_long_is_allowed():
    """The bug. `dispatch_timeout` was the only figure reported, and it cannot fire first."""
    seen = beats(connect_timeout=5, stall_timeout=60, dispatch_timeout=120)
    assert seen, "the heartbeat did not beat at all"
    _, of, ends_in = seen[0]
    assert of == 120, f"the delegation ceiling should still be reported, got {of}"
    assert ends_in <= 60, (
        f"reported {ends_in}s remaining against a stall deadline of 60s"
    )


def test_what_is_left_shrinks_as_the_turn_runs():
    """A countdown that does not count down is a constant wearing a countdown's name."""
    seen = beats(connect_timeout=5, stall_timeout=60, dispatch_timeout=120)
    assert len(seen) >= 2, f"needed two beats to compare, got {len(seen)}"
    assert seen[-1][2] < seen[0][2], (
        f"remaining did not fall across beats: {[round(s[2], 1) for s in seen]}"
    )


def test_the_ceiling_binds_when_it_is_the_tightest():
    """It is the tighter of two, not a substitution of one for another.

    Config enforces `connect_timeout <= stall_timeout <= dispatch_timeout`, so both equal
    is the shape where the delegation ceiling is what remains.
    """
    seen = beats(connect_timeout=5, stall_timeout=20, dispatch_timeout=20)
    assert seen
    _, of, ends_in = seen[0]
    assert of == 20
    assert 0 <= ends_in <= 20
