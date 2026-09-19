"""A delegation cancelled during the idle hold took a slot nothing could release.

`acquire` takes the slot inside `_try_take` and then awaits `_count_the_burst` for up to
`admission_idle_hold` seconds before returning the lease. `admit` is what releases a
lease, in the `finally` of its own `try` — and it cannot reach that `try` until `acquire`
returns. A cancellation inside the hold therefore leaves a slot with no owner. `acquire`'s
own `finally` is no help: it drops the ticket, which `_try_take` has already set to None on
the path that took the slot.

Nothing else reclaims it either. `slots.py` keys a record by `(pid, start_time)` and drops
it once that process stops, which bounds the leak to the life of the process — and the
process here is a long-lived MCP server, so "bounded" means "until the operator restarts
it". Found 2026-09-19 on a server holding `seqs: 1` with nothing running, two hours after
its last reconnect.

The cost is not one slot. Every cancellation during a hold takes another, and at
`max_inflight_seqs` of them the gate refuses every delegation for the rest of the server's
life, each one waiting out `admission_wait_timeout` and producing nothing.

The negative control is the ordinary cancellation, which was never broken: cancelled while
*holding* the lease, `admit` has its `try` and releases correctly. Both are asserted here,
because a fix that released on the wrong path would pass the first assertion alone.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 5,
        "admission_idle_hold": 10.0,
        "kv_token_budget": 100_000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


async def _cancel_inside(g: Admission, *, after: float) -> int:
    """Start a delegation, cancel it `after` seconds, and report what the gate still holds."""

    async def hold() -> None:
        async with g.admit(100, entry_key="flash", entry_limit=5):
            await asyncio.sleep(30)

    task = asyncio.create_task(hold())
    await asyncio.sleep(after)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # The release is awaited on the way out, so yield once before reading the gauge.
    await asyncio.sleep(0.05)
    return g.status()["inflight_seqs"]


@pytest.mark.asyncio
async def test_a_cancellation_during_the_idle_hold_releases_the_slot() -> None:
    # The gate is idle, so this request holds; 0.5s is well inside the 10s window.
    g = Admission(cfg())
    assert await _cancel_inside(g, after=0.5) == 0


@pytest.mark.asyncio
async def test_a_cancellation_while_holding_the_lease_still_releases() -> None:
    """Negative control: the path that always worked must keep working."""
    g = Admission(cfg(admission_idle_hold=0.0))
    assert await _cancel_inside(g, after=0.05) == 0
