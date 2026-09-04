"""Admission waited, but it did not queue.

`acquire` was a re-test loop: a waiter woke, tested the four rules, and either fitted or
waited again. There was no ticket and no ordering, so a request that had waited nine
minutes had no claim ahead of one arriving that instant. The module docstring's
"oversubscription queues rather than fails" was true about waiting and false about a place
in line.

Across processes it was worse than merely unordered. A release in another process notifies
nothing here, so cross-process waiting is polling: a waiter looks every 0.25s, and a
request arriving in the gap took the slot it had been queueing for. Raising
`admission_wait_timeout` against that buys a longer unfair wait and turns a bounded
failure into possible starvation, which is why fairness had to land first.

**Two properties, and the second is why strict ticket order would have been wrong.**
Waiters are served in order, *and* a waiter that cannot be admitted right now does not
block one that can. Strict FIFO reintroduces head-of-line blocking -- a large request
parked on the large-prefill cap would stall every small request behind it for as long as
the request ahead of it ran -- which is the same starvation
`test_a_blocked_large_prefill_holds_no_other_capacity` forbids for nested semaphores.

The ordering tests use **two `Admission` instances over one shared file**. Each has its
own condition, so neither can notify the other and both must poll: that is precisely the
cross-process case, without the cost of real subprocesses. They need real `flock` and a
real `/proc`, so they are skipped where those are absent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from claude_delegate_local.admission import (
    QUEUED_RULE,
    Admission,
    AdmissionTimedOut,
)
from claude_delegate_local.config import Config
from claude_delegate_local.slots import SharedSlots

try:
    import fcntl
except ImportError:  # pragma: no cover -- Windows
    fcntl = None  # type: ignore[assignment]

UNPROVEN = (
    "ADMISSION FAIRNESS UNPROVEN BY THIS RUN -- this is not a pass. Ordering is enforced "
    "through a real flock on a shared file, and liveness through /proc. Run it under WSL "
    "-- see CONTRIBUTING.md: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest "
    "tests/regression/test_admission_waited_without_queueing.py'"
)
posix_only = pytest.mark.skipif(
    fcntl is None or not Path("/proc").is_dir(), reason=UNPROVEN
)


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 5,
        "kv_token_budget": 100_000,
        "large_prefill_tokens": 10_000,
        "max_inflight_large_prefills": 2,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def gate_at(path: Path, **over) -> Admission:
    """One gate over the shared file. Two of these behave as two processes would."""
    slots = SharedSlots(path)
    slots.prepare()
    return Admission(cfg(**over), slots)


async def take(g: Admission, tokens: int, *, deadline: float | None = None, key="flash"):
    return await g.acquire(
        tokens,
        prefill_tokens=tokens,
        entry_key=key,
        entry_limit=5,
        deadline=deadline,
    )


async def parked(g: Admission, tokens: int, *, key: str = "flash"):
    task = asyncio.create_task(take(g, tokens, key=key))
    for _ in range(5):
        await asyncio.sleep(0)
    # A polling waiter must be given real time to come round, or "not done" would only
    # mean "has not looked yet" -- which passes against a gate that admits everything.
    await asyncio.sleep(0.4)
    return task


def waiting_in(path: Path) -> dict[str, object]:
    """Every queued ticket in the file, across records."""
    doc = json.loads(path.read_text(encoding="utf-8") or "{}")
    out: dict[str, object] = {}
    for record in (doc.get("records") or {}).values():
        out.update(record.get("waiting") or {})
    return out


# ---- the bug: a newcomer overtook a request that had been waiting ----------------


@posix_only
@pytest.mark.asyncio
async def test_a_newcomer_cannot_overtake_a_waiter_that_could_run(tmp_path):
    """The unfairness, stated exactly.

    A waiter is queued and the slot then frees. Because a release in another process
    notifies nothing, the waiter does not learn for up to a poll interval -- and a request
    arriving inside that gap used to be admitted at once, taking the slot the waiter had
    been queueing for. It must now be refused, and refused *for the queue* rather than for
    capacity, because capacity is genuinely free.
    """
    path = tmp_path / "slots.json"
    holder, waiter, newcomer = (gate_at(path, max_inflight_seqs=1) for _ in range(3))

    held = await take(holder, 100)
    queued = await parked(waiter, 100)
    assert not queued.done(), "the gate admitted two sequences past max_inflight_seqs=1"
    assert len(waiting_in(path)) == 1, "the waiter took no place in line"

    # The slot is free from here on, and only the queue should stand in the way.
    await holder.release(held)

    with pytest.raises(AdmissionTimedOut) as caught:
        await take(newcomer, 100, deadline=asyncio.get_running_loop().time())
    assert caught.value.rule == QUEUED_RULE, (
        f"refused for {caught.value.rule!r}, so this test would also pass against a gate "
        "that was simply out of capacity"
    )

    # And the waiter that had the claim does get it.
    lease = await asyncio.wait_for(queued, timeout=5)
    await waiter.release(lease)
    assert waiting_in(path) == {}


@posix_only
@pytest.mark.asyncio
async def test_the_earlier_of_two_waiters_is_served_first(tmp_path):
    """Order, not merely refusal. Both waiters fit the moment the slot frees, so nothing
    but the ticket can decide between them."""
    path = tmp_path / "slots.json"
    holder, first, second = (gate_at(path, max_inflight_seqs=1) for _ in range(3))

    held = await take(holder, 100)
    early = await parked(first, 100)
    late = await parked(second, 100)
    assert not early.done() and not late.done()

    tickets = sorted(int(t) for t in waiting_in(path))
    assert len(tickets) == 2 and tickets[0] < tickets[1], "tickets are not ordered"

    await holder.release(held)
    done, _ = await asyncio.wait({early, late}, timeout=5)
    assert done == {early}, "the later waiter was served first, or both were admitted"

    await first.release(await early)
    await second.release(await asyncio.wait_for(late, timeout=5))


# ---- the hazard this change introduces: an abandoned place in line --------------


@posix_only
@pytest.mark.asyncio
async def test_a_timed_out_waiter_gives_up_its_place(tmp_path):
    """A ticket left at the head of the queue starves every later waiter for as long as
    the process lives. So the drop is in a `finally`, not on the timeout path."""
    path = tmp_path / "slots.json"
    holder, quitter = gate_at(path, max_inflight_seqs=1), gate_at(path, max_inflight_seqs=1)

    held = await take(holder, 100)
    with pytest.raises(AdmissionTimedOut):
        await take(quitter, 100, deadline=asyncio.get_running_loop().time())
    assert waiting_in(path) == {}, "a timed-out waiter kept its place in line"

    # Proven by use, not by reading the file: a phantom ticket would block this for ever.
    await holder.release(held)
    await holder.release(await asyncio.wait_for(take(holder, 100), timeout=5))


@posix_only
@pytest.mark.asyncio
async def test_a_cancelled_waiter_gives_up_its_place(tmp_path):
    """Cancellation is the path a handler would forget, which is why `finally` covers it
    rather than an `except AdmissionTimedOut`."""
    path = tmp_path / "slots.json"
    holder, cancelled = gate_at(path, max_inflight_seqs=1), gate_at(path, max_inflight_seqs=1)

    held = await take(holder, 100)
    task = await parked(cancelled, 100)
    assert len(waiting_in(path)) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiting_in(path) == {}, "a cancelled waiter kept its place in line"

    await holder.release(held)
    await holder.release(await asyncio.wait_for(take(holder, 100), timeout=5))


# ---- the property strict FIFO would have broken ---------------------------------


@posix_only
@pytest.mark.asyncio
async def test_a_waiter_that_cannot_run_does_not_block_one_that_can(tmp_path):
    """Head-of-line blocking, refused for the shared path too.

    A large request parked on the large-prefill cap is not spending its turn, so it must
    not hold one. Strict ticket order would stall the small request behind it for as long
    as the running large request lasts -- the same starvation the four rules are checked as
    one predicate to avoid.
    """
    path = tmp_path / "slots.json"
    over = {
        "max_inflight_seqs": 2,
        "kv_token_budget": 1_000_000,
        "large_prefill_tokens": 100,
        "max_inflight_large_prefills": 1,
    }
    running, blocked, small = (gate_at(path, **over) for _ in range(3))

    held = await take(running, 500)
    stuck = await parked(blocked, 500)
    assert not stuck.done(), "the large-prefill cap did not bind"
    assert len(waiting_in(path)) == 1

    # One sequence slot is still free and this request fits every rule, so the queued
    # large one must not be counted as ahead of it.
    lease = await asyncio.wait_for(take(small, 10), timeout=5)
    await small.release(lease)

    await running.release(held)
    await blocked.release(await asyncio.wait_for(stuck, timeout=5))
