"""The empty answer, reproduced against the live cluster and then recovered from.

ADR-0014 was written from a measurement, and the guard built on it is only worth what the
reproduction is worth: every other test of this path uses a scripted backend that returns
the exhaustion signature because it was told to. These two exercise a real model deciding
on its own to spend the whole budget on reasoning, which is the only version that can
disagree with the design.

Two things keep them out of the way, and they are not the same thing. CI deselects the
`integration` marker outright, so nothing here runs there. Run locally they execute, and
the live-endpoint guard skips them if the endpoint is not genuinely reachable -- by
address, never by name (ADR-0021). The skip reason is deliberately loud about what was not
proven, because a quiet skip on a path this load-bearing is worse than no test: it reports
green for a mechanism nobody ran. `test_live_skip_is_not_readable_as_a_pass.py` enforces
that wording.

Both tests are deliberately cheap. Reproducing exhaustion needs a *small* budget, not a
large one, so neither waits out a long generation -- a full-cap max-effort exhaustion runs
for tens of minutes on this deployment (JOURNAL 2026-08-27) and has no place in a suite.
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends import openai_compat as oc

# Reuse the live-endpoint guard rather than writing a second one. It establishes
# reachability by address and never names the host, both of which are requirements
# (ADR-0021, ADR-0029) that a fresh copy here would get subtly wrong.
from tests.test_backends_openai_compat import UNPROVEN, live_model

# A hard enough prompt that a reasoning model will not answer it in a handful of tokens,
# and short enough that the prefill is negligible. The frog well is the same shape of
# problem used for the spikes: an easy answer with an edge case that invites checking.
HARD = (
    "A frog is at the bottom of a 30 foot well. Each day it climbs up 3 feet and each "
    "night it slides back 2. Prove carefully how many days it takes to escape, and state "
    "the general formula for a well of depth d with climb c and slide s, justifying the "
    "edge case."
)


def live(*, budget: int, floor: int, cap: int, effort_default: str = "low"):
    """The live endpoint with the budgets forced small, so the failure is cheap to reach.

    `max_tokens_cap` is overridden rather than configured, because the real registry entry
    caps at the model's full window and reproducing exhaustion there is a ten-minute call.

    The three numbers are separate on purpose, and getting them wrong is easy: the cap is
    applied last and to *everything*, so a cap at the first budget also pins the retry and
    the stage that was meant to be exercised is skipped instead. That is a real behaviour
    (and the reason a full-cap max-effort call costs nothing extra), but here it silently
    turned a test of the budget retry into a test of the step-down.
    """
    real_cfg, model = live_model()
    cfg = dataclasses.replace(
        real_cfg,
        max_tokens=budget,
        thinking_max_tokens_floor=floor,
        thinking_default=effort_default,
    )
    entry = dataclasses.replace(model, max_tokens_cap=cap, default_effort="")
    return cfg, entry


@pytest.mark.integration
async def test_the_live_empty_answer_is_recovered_by_the_budget_retry():
    """Reproduce, then recover. The first dispatch is given too little room to finish, so
    reasoning consumes it and the reply comes back empty at a length stop; the retry at the
    floor gives it enough and an answer arrives.

    This is the stage that keeps the effort level, and therefore the prompt prefix. It is
    also the stage that does *not* fire at high or max effort with production numbers, so
    the level here is low deliberately -- the one where a larger budget is the real fix.

    The floor is 16384 because the measurement says so: this prompt at low effort answered
    in 7033 completion tokens (JOURNAL 2026-08-27), so a floor below that truncates again
    and the recovery falls through to the step-down -- which passes a loose assertion while
    testing the wrong stage. It did exactly that at 4096 before the number was checked
    against the measurement instead of guessed.
    """
    cfg, entry = live(budget=96, floor=16384, cap=16384, effort_default="low")
    backend = oc.OpenAICompatBackend(cfg, entry)
    try:
        result = await loop.run_one_shot(cfg, entry, backend, loop.Delegation(HARD))
    finally:
        await backend.aclose()

    if result.attempts == 1:
        pytest.fail(
            "no recovery was exercised: the first dispatch already answered inside 96 "
            f"tokens (finish_reason={result.response.finish_reason!r}). The budget is "
            "meant to be too small to finish in. Raise the difficulty or lower the cap; "
            "do not relax the assertion, or this test stops reproducing anything."
        )
    assert result.response.text.strip(), (
        "the retry at the floor produced no text either. That is a real regression in the "
        f"recovery, or the floor is too low to answer in: {result.response.finish_reason!r}"
    )
    assert result.reasoning_exhausted is False, "it answered, so nothing was exhausted"
    assert result.effort == "low", "the budget retry keeps the level; only a step-down moves it"


@pytest.mark.integration
async def test_the_live_verdict_is_exhaustion_only_after_the_step_down_was_tried():
    """The terminal state, reproduced rather than simulated.

    Max effort with a budget too small at every level: the first dispatch is empty at a
    length stop, the budget stage is skipped because the cap pins it, the level steps to
    high, and that is empty too. The verdict must then be exhaustion -- and it must have
    cost more than one call, since a verdict reached without spending the mitigations is
    the exact lie ADR-0014 reserves the word against.
    """
    cfg, entry = live(budget=128, floor=128, cap=128, effort_default="max")
    backend = oc.OpenAICompatBackend(cfg, entry)
    try:
        result = await loop.run_one_shot(cfg, entry, backend, loop.Delegation(HARD))
    finally:
        await backend.aclose()

    if result.response.text.strip():
        pytest.fail(
            "the model answered this inside 128 tokens at max effort, so the failure this "
            f"test exists to reproduce did not occur (attempts={result.attempts}). The "
            "deployment's reasoning behaviour has changed; re-measure before trusting the "
            "guard, and see JOURNAL 2026-08-27 for what was measured before."
        )
    assert result.attempts > 1, (
        "exhaustion was reported without a second dispatch. The word means every "
        "mitigation was tried, so a single attempt cannot earn it."
    )
    assert result.reasoning_exhausted is True
    assert result.effort == "high", "max steps down exactly one level, to high"


def test_the_skip_reason_here_says_what_was_not_proven():
    """Runs unconditionally, including where the endpoint is unreachable.

    The two tests above are skipped by default, which means the usual failure of a test
    like this is silence. This asserts the guard they rely on is the loud one -- imported,
    not reimplemented, so a reworded copy cannot drift into a quiet skip.
    """
    assert "this is not a pass" in UNPROVEN.lower()
    assert UNPROVEN.strip().endswith("because"), (
        "the reason must continue into the specific cause; a bare prefix reads as boilerplate"
    )
