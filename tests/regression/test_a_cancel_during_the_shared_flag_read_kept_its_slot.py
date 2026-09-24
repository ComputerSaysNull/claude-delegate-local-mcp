"""A call cancelled while it read the shared burst flag kept its slot, with nobody to give it back.

`_settle_burst` runs after the slot is taken and before `acquire` returns the lease, so the
caller cannot release it. Its cross-process check is an await, and it sat outside every
guard that gives the slot back on the way out, so a cancellation delivered there left the
slot counted for the life of the process.
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
        "max_inflight_seqs": 4,
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
async def test_a_call_cancelled_during_the_shared_flag_read_gives_its_slot_back() -> None:
    g = Admission(cfg())
    first = await g.acquire(100, entry_key="flash", entry_limit=4)  # a busy gate from here on

    reading = asyncio.Event()

    async def elsewhere() -> bool:
        reading.set()
        await asyncio.sleep(30)  # a slow file lock, standing in for the real read
        return False

    g._burst_open_elsewhere = elsewhere  # type: ignore[method-assign]
    late = asyncio.create_task(g.acquire(100, entry_key="flash", entry_limit=4))
    try:
        await asyncio.wait_for(reading.wait(), timeout=2.0)
        assert g.status()["inflight_seqs"] == 2   # the slot is taken before the read

        late.cancel()
        outcome = await asyncio.gather(late, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert g.status()["inflight_seqs"] == 1, "the cancelled call's slot was never given back"
    finally:
        if not late.done():
            late.cancel()
        await asyncio.gather(late, return_exceptions=True)
        await g.release(first)
