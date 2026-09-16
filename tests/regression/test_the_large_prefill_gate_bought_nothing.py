"""The large-prefill gate cost 286 seconds of waiting on an idle cluster and bought nothing.

`max_inflight_large_prefills` admits N requests whose prompt exceeds
`large_prefill_tokens` and queues the rest. It ships 2, described as "one running plus one
staged -- pipelining, not throttling".

Measured 2026-09-12, six delegations per arm over twelve disjoint file sets so every prefill
was genuinely cold:

    limit 6   0 of 6 queued, 0.0s total wait,   mean elapsed 88.2s, last finish 155.4s
    limit 2   4 of 6 queued, 286.3s total wait, mean elapsed 98.4s, last finish 167.5s

The gate is pure overhead for this workload. It adds 286.3s of aggregate waiting, makes the
batch 12.1s slower end to end and each call 10.2s slower, and nothing gets faster. Limit 6's
155.4s is the floor the engine sets by serialising six cold prefills itself -- which is the
gate's own justification arriving from the engine rather than from the setting, and is what
makes the setting redundant rather than merely oversized.

Worse than redundant on a quiet machine. `admission_wait_timeout` fired four times on
2026-09-12, each a 1,800s wait ending in a refusal that had produced nothing, against
`kv_cache_used_fraction` of 0.031 and zero preemptions -- a 2-wide gate binding on an idle
cluster, because a large slot was held for a whole delegation rather than for its prefill.

ADR-0072 released the large half at first token to fix that, and #184 measured the result:
at 6 the gate is confirmed inert, at 2 it still queues 4 of 6. The early release did not
retire it, which is what argues for removing it rather than tuning it.

So the test is the measurement: six large prefills, against a gate configured exactly as it
ships, admit without anyone waiting.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config

pytestmark = pytest.mark.anyio

# The arm that was measured: six cold prefills, each well over any plausible threshold.
ARMS = 6
PREFILL_TOKENS = 50_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def gate(**over) -> Admission:
    """A gate configured as the server ships, apart from room for six sequences.

    Deliberately not setting the large-prefill knobs: the point is what a default
    deployment does, and a fixture that named them would be testing its own arithmetic.
    """
    # The idle hold is off here deliberately: this file measures what the gate does with
    # six arms, and a wait applied to the first of them would time the hold instead.
    kw = {"workspace_roots": (".",), "max_inflight_seqs": ARMS,
          "kv_token_budget": 10**9, "admission_idle_hold": 0.0}
    kw.update(over)
    return Admission(Config(**kw))  # type: ignore[arg-type]


async def settle(n: int = 8) -> None:
    """Let a parked acquire reach the condition and register its ticket."""
    for _ in range(n):
        await asyncio.sleep(0)


async def test_six_large_prefills_admit_without_anyone_waiting():
    """The bug. At the shipped limit of 2, four of these six park."""
    g = gate()
    leases = [
        await asyncio.wait_for(
            g.acquire(
                PREFILL_TOKENS, 
                entry_key="flash", entry_limit=ARMS,
            ),
            timeout=1,
        )
        for _ in range(ARMS)
    ]

    assert len(leases) == ARMS
    status = g.status()
    assert status["admission_wait_count"] == 0, "something queued"
    assert status["admission_wait_seconds_total"] == 0

    for lease in leases:
        await g.release(lease)


async def test_a_capacity_rule_still_queues():
    """The control, and the reason this is a removal rather than a gutting.

    Admission still has to queue: what went is the rule that queued on prefill *size*, not
    the gate itself. A change that simply admitted everything would pass the test above and
    take the token budget with it.
    """
    g = gate(max_inflight_seqs=1)
    held = await g.acquire(
        PREFILL_TOKENS,  entry_key="flash", entry_limit=ARMS
    )

    parked = asyncio.create_task(
        g.acquire(
            PREFILL_TOKENS, 
            entry_key="flash", entry_limit=ARMS,
        )
    )
    await settle()
    assert not parked.done(), "a full gate admitted a second sequence"

    await g.release(held)
    await g.release(await asyncio.wait_for(parked, timeout=1))


async def test_a_prefill_larger_than_the_budget_is_still_refused_at_once():
    """The other control. `AdmissionImpossible` is about the token budget, not the gate.

    It must survive: a request that cannot fit an empty gate should fail immediately rather
    than spend the whole wait reaching a knowable answer.
    """
    from claude_delegate_local.admission import AdmissionImpossible

    g = gate(kv_token_budget=1000)

    with pytest.raises(AdmissionImpossible):
        await g.acquire(
            10_000, entry_key="flash", entry_limit=ARMS
        )
