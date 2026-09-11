"""A waiter the newcomers outgrew was passed over for as long as they kept arriving.

`_binding` refuses a waiter whose `ahead` is non-zero, and `ahead` counts only
earlier-ticketed waiters *that currently fit*. A waiter blocked by a capacity rule does
not fit, so it is invisible to everyone behind it. That is deliberate -- strict ticket
order would reintroduce head-of-line blocking, and
`test_a_waiter_that_cannot_run_does_not_block_one_that_can` pins the intent -- but it has
no counterweight.

Whether that becomes starvation depends on **who holds the resource the waiter is blocked
on**, and the two cases are opposite:

  * `max_inflight_large_prefills` is held by other *large* requests, which take tickets
    and queue behind each other. Small newcomers stream past, but only while a large
    blocker holds the slot, and the waiter is admitted the moment it releases. Bounded,
    and `test_the_large_prefill_cap_releases_its_waiter` is here to keep it that way --
    PLAN.md line 360 names this rule, and on this rule there is nothing to fix.
  * `kv_token_budget` is held by *whoever is in flight*, including the small newcomers
    themselves. Each one that arrives keeps the waiter infeasible, so the waiter is never
    counted as ahead and never gets in. This is the real defect.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config

pytestmark = pytest.mark.asyncio


def gate(**over) -> Admission:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 6,
        "kv_token_budget": 100_000,
        "large_prefill_tokens": 10_000,
        "max_inflight_large_prefills": 2,
    }
    kw.update(over)
    return Admission(Config(**kw))  # type: ignore[arg-type]


async def take(g: Admission, tokens: int, prefill: int, key: str = "flash"):
    return await g.acquire(
        tokens, prefill_tokens=prefill, entry_key=key, entry_limit=5
    )


async def settle(n: int = 8) -> None:
    """Let a parked acquire reach the condition and register its ticket."""
    for _ in range(n):
        await asyncio.sleep(0)


async def test_a_newcomer_may_overtake_a_waiter_that_cannot_run():
    """The deliberate half, and the shape of the bug.

    Inside the grace period a waiter blocked on the budget is still invisible, and the
    newcomers -- which hold that very budget -- stream past. Each small is admitted, then
    the previous one released, so in-flight tokens never drop far enough for the waiter
    and never rise high enough to refuse a small. Left alone, this repeats forever.

    Kept as a control: a fix that refused these newcomers immediately would pass the test
    below while reintroducing the head-of-line blocking this design rejects.
    """
    g = gate(kv_token_budget=100_000, max_inflight_large_prefills=9,
             admission_starvation_grace=30.0)
    held = await take(g, 50_000, 1)
    parked = asyncio.create_task(take(g, 60_000, 60_000))
    await settle()
    assert not parked.done(), "the waiter was admitted; rule 2 never bound"

    for _ in range(5):
        nxt = await asyncio.wait_for(take(g, 50_000, 1), timeout=1)
        await g.release(held)
        held = nxt
        await settle()
    assert not parked.done(), "overtaking inside the grace period is the intended design"

    parked.cancel()
    await g.release(held)


async def test_a_waiter_left_behind_long_enough_stops_being_overtaken():
    """The fix. Past the grace period the waiter counts as ahead even while infeasible.

    That makes it a barrier: newcomers queue behind it, the in-flight work drains, and
    the budget it needs is the waiter's rather than the next arrival's.
    """
    g = gate(kv_token_budget=100_000, max_inflight_large_prefills=9,
             admission_starvation_grace=0.05)
    held = await take(g, 50_000, 1)
    parked = asyncio.create_task(take(g, 60_000, 60_000))
    await settle()
    assert not parked.done()

    await asyncio.sleep(0.15)  # age it past the grace

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(take(g, 50_000, 1), timeout=0.3)

    await g.release(held)
    lease = await asyncio.wait_for(parked, timeout=1)
    await g.release(lease)


async def test_the_large_prefill_cap_releases_its_waiter():
    """The control, and the reason the fix must not be aimed at rule 3.

    Smalls do stream past a large parked on `max_inflight_large_prefills`, but only while
    another large holds the slot. Nothing a small does keeps it there, so the wait ends.
    A fix that treated this as starvation would serialise the server for no gain.
    """
    g = gate(max_inflight_large_prefills=1, kv_token_budget=10**9)
    blocker = await take(g, 50_000, 50_000)
    parked = asyncio.create_task(take(g, 50_000, 50_000))
    await settle()
    assert not parked.done()

    for _ in range(5):
        s = await asyncio.wait_for(take(g, 100, 100), timeout=1)
        await g.release(s)
        await settle()
    assert not parked.done(), "the blocker still holds the only large slot"

    await g.release(blocker)
    lease = await asyncio.wait_for(parked, timeout=1)
    await g.release(lease)


async def test_a_newcomer_cannot_overtake_a_waiter_that_could_run():
    """The protection that already works, pinned so the fix cannot cost it.

    Once the waiter is feasible it counts as ahead, and a newcomer is refused. Any
    anti-starvation change must leave this true.
    """
    g = gate(max_inflight_seqs=1, kv_token_budget=10**9)
    held = await take(g, 100, 100)
    parked = asyncio.create_task(take(g, 100, 100))
    await settle()
    assert not parked.done()

    await g.release(held)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(take(g, 100, 100), timeout=0.3)

    lease = await asyncio.wait_for(parked, timeout=1)
    await g.release(lease)
