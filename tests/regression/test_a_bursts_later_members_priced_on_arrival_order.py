"""Every member of a burst but the first was priced at the position it arrived in.

ADR-0085 made a burst's *first* member wait before recording its concurrency, because its
own snapshot says "solo" and would go on saying so however many siblings are a millisecond
behind it. Its reasoning then stopped, at: "any later member already sees this one, so
concurrency is known and the wait would be pure latency".

That last step is false. A later member sees the siblings *ahead* of it and none of those
still arriving behind it, so `seqs_at_grant + waiting_at_grant + 1` is its own position in
the burst rather than the burst's size. Three simultaneous arrivals priced 1, 2 and 3 where
all three ran at three, and the rate memory is keyed by that number.

The fix is that a member admitted while a wait is open takes that wait's answer. One wait
serves the burst rather than one each: a second would re-count the same arrivals and bill
every member for its own window, and a joiner cannot wait inside the wait it is joining.

It costs no latency that was not already being paid. Before it the first member waited one
window alone; now the burst shares that same window, and a request joining a busy gate with
nothing open still does not wait at all — which the second test pins, because a hold that
quietly became a toll on every admission would be a worse bug than the one fixed here.
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
        # Short enough to keep the suite quick, long enough that three tasks started from
        # one gather are all admitted inside the first window.
        "admission_idle_hold": 0.05,
        "kv_token_budget": 100_000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def _priced_at(lease) -> int:
    """The number `server.run_delegation` derives from a lease, and the rate memory's key."""
    return lease.seqs_at_grant + lease.waiting_at_grant + 1


@pytest.mark.asyncio
async def test_every_member_of_a_burst_prices_on_the_whole_burst() -> None:
    g = Admission(cfg())

    leases = await asyncio.gather(
        *(g.acquire(100, entry_key="flash", entry_limit=5) for _ in range(3))
    )
    try:
        assert [_priced_at(x) for x in leases] == [3, 3, 3]
    finally:
        for lease in leases:
            await g.release(lease)


@pytest.mark.asyncio
async def test_a_later_arrival_does_not_wait_when_nothing_is_open() -> None:
    """Negative control: joining must not become a toll on every busy-gate admission."""
    g = Admission(cfg(admission_idle_hold=5.0))

    first = await g.acquire(100, entry_key="flash", entry_limit=5)
    # The first member's own wait is over by the time its acquire returned, so the gate is
    # busy with nothing open. A second arrival must be admitted straight away.
    second = await asyncio.wait_for(
        g.acquire(100, entry_key="flash", entry_limit=5), timeout=1.0
    )

    await g.release(first)
    await g.release(second)
