"""Two openers in one process overwrote each other's burst wait, and whoever joined the first hung.

`_settle_burst` read `_holding`, awaited `_burst_open_elsewhere`, then wrote `_holding`. A
second opener crossing that await overwrote the first opener's future, `_settle_waiters`
then settled whichever future was current, and a call that had joined the first one waited,
holding its slot, until its client gave up.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 6,
        "admission_idle_hold": 0.2,
        "kv_token_budget": 100_000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met before deadline")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_call_that_joined_the_first_of_two_openers_is_settled() -> None:
    g = Admission(cfg())

    # Something already in flight, so every later arrival sees a busy gate and takes the
    # path with the await in it. Its own hold (the gate was idle for it) ends first.
    first = await g.acquire(100, entry_key="flash", entry_limit=6)
    assert g._holding is None

    # The first caller to ask is held at the await; everyone after answers at once.
    let_b_go = asyncio.Event()
    calls = 0

    async def elsewhere() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            await let_b_go.wait()
        return True

    g._burst_open_elsewhere = elsewhere  # type: ignore[method-assign]

    async def member() -> object:
        return await g.acquire(100, entry_key="flash", entry_limit=6)

    tasks: list[asyncio.Task] = []
    try:
        b = asyncio.create_task(member())
        tasks.append(b)
        await _wait_until(lambda: calls == 1)          # B is parked inside the await

        a = asyncio.create_task(member())
        tasks.append(a)
        await _wait_until(lambda: g._holding is not None)   # A opened a wait
        a_wait = g._holding

        j = asyncio.create_task(member())
        tasks.append(j)
        await _wait_until(lambda: g.status()["inflight_seqs"] == 4)
        await asyncio.sleep(0.05)                        # J is parked on A's wait

        let_b_go.set()                                   # B comes back from the await
        await asyncio.sleep(0.02)
        assert g._holding is None or g._holding is a_wait, (
            "the second opener replaced the first opener's wait"
        )

        done, pending = await asyncio.wait(tasks, timeout=3.0)
        assert j in done, "the call that joined the first opener's wait was never settled"
        assert not pending, "an opener or joiner never came back"
        for task in done:
            await g.release(task.result())
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await g.release(first)
