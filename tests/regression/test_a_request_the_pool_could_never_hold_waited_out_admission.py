"""A request the KV pool could never hold waited out admission instead of being refused.

`acquire` refuses up front a delegation larger than the whole budget, because queueing it
would spend the entire wait reaching an answer knowable at once -- and report it as
congestion, which is the one thing it is not. That refusal compared the estimate with the
*configured* `kv_token_budget`.

The gate admits against `effective_token_budget`, the lower of that setting and the KV pool
the endpoint reports (`observe_pool`), because a budget above the pool over-admits and
queues rather than erroring. Once a smaller pool is recorded the two numbers disagree, and
a request between them passes the up-front refusal and then can never satisfy the token
rule: it polls until its deadline expires, or for ever when there is none, and is reported
as a timeout -- saturation -- when the truth was that it never fit.

The message has to name the limit that actually binds. Telling an operator to raise
`DELEGATE_KV_TOKEN_BUDGET` when the pool is the lower ceiling sends them to the one knob
that cannot help.

Named after the bug, per the project's convention.
"""

import asyncio

import pytest

from claude_delegate_local.admission import Admission, AdmissionImpossible
from claude_delegate_local.config import Config

# The shipped budget and the pool the endpoint actually reported, so the fixture stays tied
# to the observation rather than to round numbers that mean nothing. The hopeless request
# sits between them: above the pool, under the configured budget.
SHIPPED_BUDGET = 2_400_000
REAL_POOL = 1_467_988
BETWEEN = 2_000_000


def gate(**over) -> Admission:
    kw = {
        "workspace_roots": (".",),
        "admission_idle_hold": 0.0,
        "kv_token_budget": SHIPPED_BUDGET,
    }
    kw.update(over)
    return Admission(Config(**kw))  # type: ignore[arg-type]


async def test_a_size_the_pool_could_never_hold_is_refused_at_once():
    """The bug. The refusal has to test the ceiling the gate actually admits against."""
    g = gate()
    g.observe_pool(REAL_POOL)
    assert g.effective_token_budget == REAL_POOL

    # No deadline, so on the unfixed code this never returns; `wait_for` bounds it so the
    # test fails on the timeout rather than hanging the suite.
    with pytest.raises(AdmissionImpossible) as refused:
        await asyncio.wait_for(
            g.acquire(BETWEEN, entry_key="flash", entry_limit=5), timeout=2
        )

    message = str(refused.value)
    assert str(REAL_POOL) in message, message
    assert "kv_cache_size_tokens" in message, message
    # Not congestion: nothing was ever waited on.
    assert g.status()["admission_timeouts"] == 0


async def test_a_size_the_pool_can_hold_is_still_admitted():
    """The control. Refusing anything merely large would pass the test above."""
    g = gate()
    g.observe_pool(REAL_POOL)

    lease = await asyncio.wait_for(
        g.acquire(REAL_POOL, entry_key="flash", entry_limit=5), timeout=2
    )
    await g.release(lease)


async def test_a_size_above_the_configured_budget_still_names_that_setting():
    """The other control. With no pool reported, the operator's knob is what binds, and
    the message must keep pointing at it."""
    g = gate(kv_token_budget=100_000)

    with pytest.raises(AdmissionImpossible) as refused:
        await asyncio.wait_for(
            g.acquire(500_000, entry_key="flash", entry_limit=5), timeout=2
        )

    message = str(refused.value)
    assert "DELEGATE_KV_TOKEN_BUDGET" in message, message
    assert "100000" in message, message
