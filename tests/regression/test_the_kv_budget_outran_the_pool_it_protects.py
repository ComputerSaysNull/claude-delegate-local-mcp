"""`kv_token_budget` was 1.64x the KV pool it exists to sit under.

The setting describes itself as sitting "just under the measured KV pool". It ships
2,400,000; the endpoint reports `kv_cache_size_tokens` of **1,467,988**, read again on
2026-09-13 and unchanged. Nothing had failed, which is exactly why it drifted unnoticed:
over-admitting queues and preempts rather than erroring, so the setting protects latency and
never announces that it has stopped doing so. The likely cause is the 2026-09-04 model swap
— a vision model carries more weights, so less memory is left for KV — against a constant
measured on the model before it.

A new constant would drift the same way the next time the pool moves. So the effective
budget is the lower of the operator's number and the pool the endpoint reports.

**This is not the silent override `WindowCheck` refuses.** That class validates and never
derives, because `context_window` is the operator's claim about a *model* and quietly
adopting the server's figure would override them. Here both numbers are *ceilings* on the
same physical thing, and taking the lower of two ceilings overrides neither: the operator's
number still caps, and the hardware also caps. PLAN.md cited `WindowCheck` as a derivation
precedent, which it is not, and the distinction is the reason this is allowed at all.

The reading arrives free. `seed_decode_rate` already probes the cluster on the dispatch path
to price the first turn, and `kv_cache_size_tokens` comes back in the same payload and was
discarded.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config

# The two numbers this test is named for, used as its fixture so they stay tied to the
# observation rather than becoming round ones that mean nothing.
SHIPPED_BUDGET = 2_400_000
REAL_POOL = 1_467_988


def gate(**over) -> Admission:
    kw = {"workspace_roots": (".",), "kv_token_budget": SHIPPED_BUDGET}
    kw.update(over)
    return Admission(Config(**kw))  # type: ignore[arg-type]


def test_a_pool_smaller_than_the_budget_lowers_it():
    """The bug. The shipped budget is 1.64x the pool it claims to sit under."""
    g = gate()
    assert g.effective_token_budget == SHIPPED_BUDGET

    g.observe_pool(REAL_POOL)

    assert g.effective_token_budget == REAL_POOL


def test_a_pool_larger_than_the_budget_does_not_raise_it():
    """The control that stops this becoming a silent override.

    The operator's number is a ceiling they chose. A bigger machine does not entitle the
    server to spend more than they allowed, and a fix that simply adopted the endpoint's
    figure would pass the test above while doing exactly that.
    """
    g = gate(kv_token_budget=100_000)

    g.observe_pool(REAL_POOL)

    assert g.effective_token_budget == 100_000


def test_an_unreadable_pool_leaves_the_configured_value_standing():
    """The other control. A monitoring read must never tighten a gate to nothing.

    `seed_decode_rate` swallows every failure by design, so `None` is the ordinary case on
    an endpoint that publishes no metrics -- and a budget of zero would refuse every
    delegation, which is the failure worth proving absent.
    """
    g = gate()

    for unreadable in (None, 0, -1):
        g.observe_pool(unreadable)
        assert g.effective_token_budget == SHIPPED_BUDGET, unreadable


def test_the_gate_binds_on_the_lowered_budget():
    """The mechanism, not just the number: the rule has to read the effective value."""
    g = gate()
    g.observe_pool(REAL_POOL)

    assert g.would_bind_on_tokens(REAL_POOL + 1) == "kv_token_budget"
    assert g.would_bind_on_tokens(REAL_POOL - 1) is None


def test_both_numbers_are_reported():
    """A lowered ceiling nobody can see is the silent override this is meant not to be."""
    g = gate()
    g.observe_pool(REAL_POOL)

    status = g.status()
    assert status["kv_token_budget"] == SHIPPED_BUDGET
    assert status["kv_token_budget_effective"] == REAL_POOL
    assert status["kv_cache_size_tokens_seen"] == REAL_POOL
