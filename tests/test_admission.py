"""The four-rule gate. ADR-0012.

Every rule here is tested by *watching a request block*, never by watching one finish.
"It eventually returned" passes just as well against a gate that admits everything, which
is exactly the bug worth catching -- so each test parks a second acquire in a background
task, asserts it has not completed while the first holds its slot, and only then releases.

The rule-3 tests are the ones that separate this design from the obvious wrong one. Three
semaphores acquired in turn would let a request hold the sequence slot while blocked on
the large-prefill cap; `test_a_blocked_large_prefill_holds_no_other_capacity` is what
fails against that.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import admission as adm
from claude_delegate_local.admission import (
    Admission,
    AdmissionImpossible,
    AdmissionTimedOut,
)
from claude_delegate_local.config import Config


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


def gate(**over) -> Admission:
    return Admission(cfg(**over))


async def take(
    g: Admission,
    tokens: int,
    *,
    prefill: int | None = None,
    key: str = "flash",
    limit: int = 5,
):
    """Acquire. `prefill` defaults to the whole estimate, which is the pessimistic case."""
    return await g.acquire(
        tokens,
        prefill_tokens=tokens if prefill is None else prefill,
        entry_key=key,
        entry_limit=limit,
    )


async def parked(
    g: Admission,
    tokens: int,
    *,
    prefill: int | None = None,
    key: str = "flash",
    limit: int = 5,
):
    """Start an acquire in the background and confirm it is actually blocked.

    Returns the task. The caller must assert on `task.done()` *before* releasing
    anything -- that assertion is the whole test in every rule case below.
    """
    task = asyncio.create_task(take(g, tokens, prefill=prefill, key=key, limit=limit))
    # Enough loop turns for a gate that was going to admit it to have done so.
    for _ in range(5):
        await asyncio.sleep(0)
    return task


# ---- the four rules, each binding on its own ------------------------------------
@pytest.mark.asyncio
async def test_rule_one_total_sequences_binds() -> None:
    g = gate(max_inflight_seqs=1)
    first = await take(g, 100)
    second = await parked(g, 100)

    assert not second.done(), "a second sequence was admitted past max_inflight_seqs=1"

    await g.release(first)
    lease = await asyncio.wait_for(second, timeout=1)
    await g.release(lease)


@pytest.mark.asyncio
async def test_rule_two_token_budget_binds() -> None:
    # Two requests well under the sequence cap, whose tokens together exceed the budget.
    g = gate(max_inflight_seqs=5, kv_token_budget=1000, large_prefill_tokens=10_000)
    first = await take(g, 600)
    second = await parked(g, 600)

    assert not second.done(), "summed tokens were allowed past kv_token_budget"

    await g.release(first)
    await g.release(await asyncio.wait_for(second, timeout=1))


@pytest.mark.asyncio
async def test_rule_three_large_prefills_binds() -> None:
    # Both are large, and *nothing else* can be what blocks the second: the sequence cap
    # and the token budget both have ample room.
    g = gate(
        max_inflight_seqs=5,
        kv_token_budget=1_000_000,
        large_prefill_tokens=100,
        max_inflight_large_prefills=1,
    )
    first = await take(g, 500)
    second = await parked(g, 500)

    assert not second.done(), "a second large prefill was admitted past a cap of 1"

    await g.release(first)
    await g.release(await asyncio.wait_for(second, timeout=1))


@pytest.mark.asyncio
async def test_rule_four_endpoint_concurrency_binds() -> None:
    g = gate(max_inflight_seqs=10, kv_token_budget=1_000_000)
    first = await take(g, 100, key="flash", limit=1)
    second = await parked(g, 100, key="flash", limit=1)

    assert not second.done(), "an endpoint's own concurrency limit was not enforced"

    await g.release(first)
    await g.release(await asyncio.wait_for(second, timeout=1))


@pytest.mark.asyncio
async def test_endpoint_concurrency_is_per_endpoint() -> None:
    """A full endpoint must not block a different one -- that would make it a global cap."""
    g = gate(max_inflight_seqs=10, kv_token_budget=1_000_000)
    held = await take(g, 100, key="flash", limit=1)

    other = await asyncio.wait_for(take(g, 100, key="thinker", limit=1), timeout=1)

    await g.release(held)
    await g.release(other)


# ---- the gate must not bind when it should not ----------------------------------
@pytest.mark.asyncio
async def test_a_gate_with_room_admits_without_waiting() -> None:
    """Guards the opposite failure: a gate that serialises everything also passes rule tests."""
    g = gate(max_inflight_seqs=5, kv_token_budget=1_000_000, large_prefill_tokens=10_000)
    leases = [await take(g, 100) for _ in range(3)]

    assert g.status()["admission_wait_count"] == 0
    assert g.status()["inflight_seqs"] == 3

    for lease in leases:
        await g.release(lease)


@pytest.mark.asyncio
async def test_a_blocked_large_prefill_holds_no_other_capacity() -> None:
    """The nested-semaphore bug, stated as a test.

    A large request blocked on rule 3 must not be holding a sequence slot while it waits.
    Acquire-then-block-on-the-next-rule would fail this: the parked large request would
    have taken the only sequence slot, and the small one behind it -- which fits every
    rule -- would never run.
    """
    g = gate(
        max_inflight_seqs=2,
        kv_token_budget=1_000_000,
        large_prefill_tokens=100,
        max_inflight_large_prefills=1,
    )
    running_large = await take(g, 500)
    blocked_large = await parked(g, 500)
    assert not blocked_large.done()

    # One sequence slot is used by `running_large`. The blocked one must not be holding
    # the other, so this small request has somewhere to go.
    small = await asyncio.wait_for(take(g, 10), timeout=1)

    await g.release(small)
    await g.release(running_large)
    await g.release(await asyncio.wait_for(blocked_large, timeout=1))


# ---- release, and the paths that could leak a slot -------------------------------
@pytest.mark.asyncio
async def test_admit_releases_when_the_body_raises() -> None:
    g = gate(max_inflight_seqs=1)

    with pytest.raises(RuntimeError):
        async with g.admit(100, prefill_tokens=100, entry_key="flash", entry_limit=5):
            raise RuntimeError("the delegation failed")

    assert g.status()["inflight_seqs"] == 0
    # Proven by use, not just by reading the counter: the next acquire would block for
    # ever against a leaked slot.
    await g.release(await asyncio.wait_for(take(g, 100), timeout=1))


@pytest.mark.asyncio
async def test_release_clears_every_counter() -> None:
    g = gate(large_prefill_tokens=100)
    lease = await take(g, 5000, key="flash", limit=5)
    await g.release(lease)

    live = g.status()
    assert live["inflight_seqs"] == 0
    assert live["inflight_tokens"] == 0
    assert live["inflight_large_prefills"] == 0
    assert live["per_entry"]["flash"]["inflight"] == 0


# ---- timeouts and impossibilities -------------------------------------------------
@pytest.mark.asyncio
async def test_a_passed_deadline_times_out_and_admits_nothing() -> None:
    g = gate(max_inflight_seqs=1)
    held = await take(g, 100)

    with pytest.raises(AdmissionTimedOut):
        await g.acquire(100, prefill_tokens=100, entry_key="flash", entry_limit=5, deadline=0.0)

    # A waiter that gave up must not have partially admitted itself.
    assert g.status()["inflight_seqs"] == 1
    assert g.status()["admission_timeouts"] == 1
    await g.release(held)


@pytest.mark.asyncio
async def test_a_timeout_names_the_rule_that_bound() -> None:
    """'Waited 600s' tells an operator nothing about which limit to change."""
    g = gate(max_inflight_seqs=1)
    held = await take(g, 100)

    with pytest.raises(AdmissionTimedOut) as caught:
        await g.acquire(100, prefill_tokens=100, entry_key="flash", entry_limit=5, deadline=0.0)

    assert caught.value.rule == "max_inflight_seqs"
    assert caught.value.limit == 1
    assert "max_inflight_seqs" in str(caught.value)
    await g.release(held)


@pytest.mark.asyncio
async def test_a_request_that_fits_is_admitted_even_past_its_deadline() -> None:
    """The deadline is checked only after the predicate, and this is why.

    Refusing a request that would have run immediately makes the gate the cause of the
    outage it exists to prevent.
    """
    g = gate()
    lease = await asyncio.wait_for(
        g.acquire(100, prefill_tokens=100, entry_key="flash", entry_limit=5, deadline=0.0),
        timeout=1,
    )
    await g.release(lease)
    assert g.status()["admission_timeouts"] == 0


@pytest.mark.asyncio
async def test_a_request_larger_than_the_whole_budget_is_refused_at_once() -> None:
    """Queueing it would spend the entire wait reaching a failure knowable immediately."""
    g = gate(kv_token_budget=1000)
    with pytest.raises(AdmissionImpossible):
        await g.acquire(5000, prefill_tokens=5000, entry_key="flash", entry_limit=5, deadline=0.0)
    # Reported as impossible, not as congestion -- which is the one thing it is not.
    assert g.status()["admission_timeouts"] == 0


# ---- what the operator reads ------------------------------------------------------
@pytest.mark.asyncio
async def test_high_water_marks_survive_release() -> None:
    """The bug: reporting the live counter instead of a separately tracked peak."""
    g = gate(max_inflight_seqs=5, kv_token_budget=1_000_000, large_prefill_tokens=100)
    a = await take(g, 400)
    b = await take(g, 600)
    await g.release(a)
    await g.release(b)

    live = g.status()
    assert live["inflight_seqs"] == 0
    assert live["peak_inflight_seqs"] == 2
    assert live["peak_inflight_tokens"] == 1000
    assert live["peak_inflight_large_prefills"] == 2
    assert live["per_entry"]["flash"]["peak"] == 2


@pytest.mark.asyncio
async def test_a_wait_is_counted_and_timed() -> None:
    g = gate(max_inflight_seqs=1)
    first = await take(g, 100)
    second = await parked(g, 100)
    # Long enough to survive the millisecond rounding `status` reports at. A wait too
    # short to measure is a real wait, but it cannot show that the clock is wired up.
    await asyncio.sleep(0.05)
    await g.release(first)
    await g.release(await asyncio.wait_for(second, timeout=1))

    live = g.status()
    assert live["admission_wait_count"] == 1
    assert live["admission_wait_seconds_max"] >= 0.05
    assert live["admission_wait_seconds_total"] >= live["admission_wait_seconds_max"]


@pytest.mark.asyncio
async def test_a_long_wait_keeps_ticking(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0018, one layer earlier.

    A queued delegation runs no turns, so nothing else resets the client's idle timer.
    Without this the tick is silently absent and a merely-queued delegation is abandoned
    by the caller.
    """
    monkeypatch.setattr(adm, "_WAIT_TICK_SECONDS", 0.01)
    ticks = 0

    async def on_wait() -> None:
        nonlocal ticks
        ticks += 1

    g = gate(max_inflight_seqs=1)
    held = await take(g, 100)
    waiter = asyncio.create_task(
        g.acquire(100, prefill_tokens=100, entry_key="flash", entry_limit=5, on_wait=on_wait)
    )
    await asyncio.sleep(0.1)

    assert ticks > 1, f"a parked request pinged {ticks} times; the client would time out"

    await g.release(held)
    await g.release(await asyncio.wait_for(waiter, timeout=1))


@pytest.mark.asyncio
async def test_a_reply_allowance_does_not_make_a_request_a_large_prefill() -> None:
    """The reply is decode, not prefill, and classifying on it bounds the whole server.

    `max_tokens` defaults to 65536 against a 32768 threshold, so folding the reply
    allowance into the classification makes *every* delegation a large prefill and rule 3
    silently caps the server at `max_inflight_large_prefills` -- while every other rule
    reads as though it were the one doing the bounding.
    """
    g = gate(
        max_inflight_seqs=10,
        kv_token_budget=1_000_000,
        large_prefill_tokens=32_768,
        max_inflight_large_prefills=1,
    )
    # A short prompt with a large reply allowance: 100 tokens of prefill, 65_636 of KV.
    first = await take(g, 65_636, prefill=100)
    second = await asyncio.wait_for(take(g, 65_636, prefill=100), timeout=1)

    assert g.status()["inflight_large_prefills"] == 0
    await g.release(first)
    await g.release(second)


@pytest.mark.asyncio
async def test_the_token_budget_still_counts_the_reply_allowance() -> None:
    """The other half of the split: the reply occupies KV even though it is not prefill."""
    g = gate(max_inflight_seqs=10, kv_token_budget=1000, large_prefill_tokens=100_000)
    first = await take(g, 600, prefill=10)
    second = await parked(g, 600, prefill=10)

    assert not second.done(), "the reply allowance was left out of the KV budget"

    await g.release(first)
    await g.release(await asyncio.wait_for(second, timeout=1))
