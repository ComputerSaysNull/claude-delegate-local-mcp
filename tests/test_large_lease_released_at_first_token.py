"""The large half of a lease is given back when its prefill ends, not when the run does.

`max_inflight_large_prefills` exists to stop the engine being asked for several cold
prefills at once. `admit()` took the slot on the opening estimate and returned it in the
context manager's `finally`, so it was held for the whole delegation -- every turn, every
retry -- while the prefill it protects is over as soon as decoding starts.

Measured 2026-09-12: time to first token 64.1s against delegations running 271-847s, and
2,100s for the two that died still holding a slot. Two passes of a six-way fan-out waited
out the full 1,800s admission timeout behind that gate, on a cluster at 3% KV with zero
preemptions. (ADR-0072)

Every test here watches a *blocked* request, never a finished one: "it eventually returned"
passes against a gate that admits everything, which is the bug worth catching.
"""

from __future__ import annotations

import asyncio

import pytest
from test_admission import gate

from claude_delegate_local.admission import Admission

pytestmark = pytest.mark.anyio

LARGE = 20_000  # above the 10_000 threshold the shared cfg sets


async def _blocked(g: Admission, tokens: int = LARGE):
    """A second large acquire, parked in the background. Returns (task, lease-or-None)."""
    task = asyncio.create_task(
        g.acquire(tokens, prefill_tokens=tokens, entry_key="m", entry_limit=99)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


async def test_the_large_slot_comes_back_at_first_token():
    """The whole point. One large prefill in flight, its slot freed, the next admitted."""
    g = gate(max_inflight_large_prefills=1)
    first = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)

    waiter = await _blocked(g)
    assert not waiter.done(), "rule 3 must block a second large prefill while one is held"

    await g.release_large(first)
    second = await asyncio.wait_for(waiter, timeout=1)

    assert second.is_large
    await g.release(first)
    await g.release(second)


async def test_the_sequence_and_its_tokens_are_still_held():
    """Only the prefill counter is given back. The request is still running.

    Releasing the whole lease at first token would be a different and much worse change:
    the sequence and its token estimate describe work still in flight, and a gate that
    forgot them would over-admit against the KV budget.
    """
    g = gate(max_inflight_large_prefills=1, max_inflight_seqs=1)
    first = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)

    await g.release_large(first)

    waiter = await _blocked(g, tokens=100)
    assert not waiter.done(), "the sequence slot must still be held after the large one"

    waiter.cancel()
    await g.release(first)


async def test_releasing_the_large_half_twice_does_not_free_a_slot_it_never_held():
    """Idempotent, because the arrival that triggers it fires on every frame.

    Without this the counter would run downward on a talkative delegation and the gate
    would stop binding at all -- which is the same permanent drift the lease's own
    docstring guards against in the other direction.
    """
    g = gate(max_inflight_large_prefills=1)
    first = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)

    await g.release_large(first)
    await g.release_large(first)
    await g.release_large(first)
    await g.release(first)

    assert g.status()["inflight_large_prefills"] == 0
    # And the gate still binds: a counter driven negative would admit two here.
    a = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)
    waiter = await _blocked(g)
    assert not waiter.done(), "the gate must still bind after a repeated early release"
    waiter.cancel()
    await g.release(a)


async def test_the_full_release_does_not_subtract_the_large_half_again():
    """`release` runs in a `finally` and cannot know the early one happened. It must ask."""
    g = gate(max_inflight_large_prefills=1)
    first = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)
    await g.release_large(first)
    await g.release(first)

    assert g.status()["inflight_large_prefills"] == 0

    second = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)
    waiter = await _blocked(g)
    assert not waiter.done(), "a double subtraction would leave the gate unable to bind"
    waiter.cancel()
    await g.release(second)


async def test_the_loop_passes_the_caller_s_hook_through_to_the_adapter():
    """The middle of the chain, which the two ends cannot prove between them.

    The adapter fires `on_token` and admission frees a slot when asked; neither says the
    loop connects them. It has its own `token_arrived` for the stall deadline, so the
    failure this catches is a loop that keeps the signal to itself -- which would leave the
    lease held for the whole delegation with every unit test still green.
    """
    import test_loop as tl

    class Emitting(tl.ScriptedBackend):
        """A backend that streams, which is the condition the whole chain rests on."""

        async def complete(self, request, *, on_token=None):
            if on_token is not None:
                on_token()
            return await tl.ScriptedBackend.complete(self, request)

    seen: list[int] = []

    await tl.loop.run_one_shot(
        tl.cfg(), tl.entry(), Emitting([tl.ok_response("done")]), tl.loop.Delegation("hi"),
        on_token=lambda: seen.append(1),
    )

    assert seen, "the loop must forward token arrival to its caller, not only to itself"


async def test_a_small_lease_has_no_large_half_to_release():
    """A no-op rather than an error, so a caller need not ask whether it was large."""
    g = gate(max_inflight_large_prefills=1)
    small = await g.acquire(100, prefill_tokens=100, entry_key="m", entry_limit=99)

    await g.release_large(small)

    assert g.status()["inflight_large_prefills"] == 0
    a = await g.acquire(LARGE, prefill_tokens=LARGE, entry_key="m", entry_limit=99)
    waiter = await _blocked(g)
    assert not waiter.done(), "the small release must not have freed a large slot"
    waiter.cancel()
    await g.release(a)
    await g.release(small)
