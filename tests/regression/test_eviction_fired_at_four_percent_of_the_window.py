"""Thirty tool results were stubbed at 3.9% of a 1M window, against a 50% threshold.

`_OverflowGuard.evict_upto` gates its pressure check on `context_overflow_enabled`. The
setting ships `False`, and unarmed the guard skips pressure entirely and evicts on *count*
alone -- `keep_tool_results`, which ships 6. So the threshold the design is built around
never applied to the default configuration at all.

Measured 2026-09-13 on one delegation: 36 tool results, 30 evictions, a largest prompt of
41,016 tokens against a 1,048,576-token window -- **3.9%**, where `OVERFLOW_EVICT_AT` is
**0.50**. Retaining the entire history would have cost about 104,730 tokens, roughly 10% of
the window. Nothing needed evicting.

Off-by-default is *correct* where the window was inherited: every threshold is measured
against `ModelEntry.context_window`, and arming against a number nobody chose would compute
each one from a guess. That reason does not apply when the operator set it, and
`context_window_defaulted` already records which case this is.

The control matters as much as the fix. Arming this against a *defaulted* window is exactly
what the setting exists to prevent, so a defaulted entry must behave as it did before --
and that test must pass both before and after.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest
from test_loop import cfg

from claude_delegate_local import loop
from claude_delegate_local.registry import ModelEntry

# The measurement this test is named for, used as its fixture so the numbers stay tied to
# the observation rather than becoming round ones that mean nothing.
WINDOW = 1_048_576
PROMPT_TOKENS = 41_016      # the largest prompt in the run: 3.9% of the window
RESULTS = 36                # tool results the run accumulated
HISTORY_TOKENS = 104_730    # what retaining all 36 would have cost

# Sizes, because eviction is driven by them since ADR-0079. Split evenly here: this file is
# about the *pressure gate*, and an even split keeps the total tied to the measurement
# without the spread confounding it. The spread is exercised where it belongs, in
# test_eviction_priced_a_refusal_like_a_50kb_file.py.
SIZES = [HISTORY_TOKENS // RESULTS] * RESULTS


def entry(*, defaulted: bool) -> ModelEntry:
    return ModelEntry(
        key="k", served_model_id="served-id-1", base_url="http://example.com:8000",
        api_format="openai", context_window=WINDOW, context_window_defaulted=defaulted,
    )


def guard_at(prompt_tokens: int, *, defaulted: bool) -> loop._OverflowGuard:
    g = loop._OverflowGuard(cfg(), entry(defaulted=defaulted))
    g.prev_input_tokens = prompt_tokens
    return g


def test_a_window_the_operator_set_is_not_evicted_at_four_percent():
    """The bug. Unarmed, the guard never looks at pressure and stubs on count alone."""
    g = guard_at(PROMPT_TOKENS, defaulted=False)

    assert g.share() < loop.OVERFLOW_EVICT_AT, "the fixture must sit below the threshold"
    assert g.evict_upto(SIZES) == 0


def test_a_defaulted_window_still_evicts_on_count():
    """The control, and it must pass before the fix as well as after.

    Arming against a window nobody chose is the thing off-by-default exists to prevent, so
    this behaviour is deliberately unchanged. Without it, the fix above would pass just as
    well by arming everything -- which is the wrong fix and looks identical from the bug's
    side.
    """
    g = guard_at(PROMPT_TOKENS, defaulted=True)

    assert g.evict_upto(SIZES) > 0


def test_real_pressure_on_a_set_window_still_evicts():
    """The other control. A guard that never evicts would pass the first test too.

    Past the threshold the history is genuinely too big, and this is the case the whole
    mechanism exists for.
    """
    g = guard_at(int(WINDOW * 0.8), defaulted=False)

    assert g.share() > loop.OVERFLOW_EVICT_AT
    assert g.evict_upto(SIZES) > 0


def test_the_boundary_never_retreats_once_pressure_passes():
    """Un-stubbing would rewrite the history in the other direction at the same cost.

    ADR-0056's whole point: the serving stack caches prefixes, so every move of the first
    difference discards everything after it. Evicting and then un-evicting pays twice.
    """
    g = guard_at(int(WINDOW * 0.8), defaulted=False)
    under_pressure = g.evict_upto(SIZES)
    assert under_pressure > 0

    g.prev_input_tokens = PROMPT_TOKENS  # pressure falls away again
    assert g.evict_upto(SIZES) == under_pressure


@pytest.mark.parametrize("keep", [16])
def test_the_retained_count_is_sized_from_what_a_run_actually_holds(keep):
    """Six retained 2.71% of the window where the whole history cost about 10%.

    A count is the wrong unit for this -- the 36 results ranged from 200 bytes to 50,068,
    so it prices a one-line refusal and a 50KB file identically -- and that is filed rather
    than fixed here. What this pins is that the default is no longer sized for a history
    four times smaller than the one measured.
    """
    assert cfg().keep_tool_results == keep
