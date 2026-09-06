"""The model was handed a reply budget no deadline in the system could pay for.

Measured 2026-09-05 from the operator transcripts. `effort: high` raises `max_tokens` to
`thinking_max_tokens_floor` = 131,072. The cluster decodes a single stream at ~36 tok/s, so
that budget is about 77 minutes of generation, against a `stall_timeout` of 2,100 s. Three
`docs-audit-local` runs died at exactly 2100.0 s with `turns: null` and read as wedged; one
of them had produced 34,276 output tokens in 1,132 s on a straight line and was still going.

The two settings are in different units and nothing related them, which is why this
survived: comparing them needs a decode rate, and a decode rate has to be measured. ADR-0055
measures it and derives the bound.

The control below is the point of the file. It asserts the *old* arithmetic is still
unreachable -- that a 131,072-token budget genuinely cannot be decoded inside the deadline
at the measured rate. Without it, a bug in the ceiling that returned some number would look
like a passing test, and this project has already found four checks that could not fail.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import CanonicalResponse
from claude_delegate_local.config import Config
from claude_delegate_local.loop import DecodeRate, resolve_max_tokens
from claude_delegate_local.registry import ModelEntry

# Measured on this deployment, JOURNAL 2026-09-05: ~36 tok/s for one stream with nothing
# else decoding, falling to 19-26 per sequence once two to five share the machine.
SOLO_TOKENS_PER_SECOND = 36.2
SHARED_TOKENS_PER_SECOND = 21.2


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": "http://example.com:8000",
          "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


def test_the_old_budget_really_was_unreachable() -> None:
    """The control. If this ever fails, every other test in the file proves nothing.

    Passing `ceiling=None` is the behaviour before ADR-0055, reproduced rather than
    described: the budget comes back at the effort floor, and decoding it at the measured
    rate overruns the longest deadline in the system.
    """
    config = cfg()
    budget = resolve_max_tokens(config, entry(), "high", ceiling=None)

    assert budget == config.thinking_max_tokens_floor == 131072
    seconds_needed = budget / SOLO_TOKENS_PER_SECOND
    assert seconds_needed > config.stall_timeout, (
        f"the control is broken: {budget} tokens at {SOLO_TOKENS_PER_SECOND} tok/s needs "
        f"{seconds_needed:.0f}s, which no longer exceeds stall_timeout "
        f"({config.stall_timeout}s), so this file can no longer tell the bug from the fix"
    )


@pytest.mark.parametrize("rate", [SOLO_TOKENS_PER_SECOND, SHARED_TOKENS_PER_SECOND])
def test_the_budget_handed_to_the_model_fits_the_deadline(rate: float) -> None:
    """The invariant the whole change exists to establish, at both measured rates."""
    config = cfg()
    ceiling = DecodeRate(rate).ceiling(config, config.stall_timeout)
    budget = resolve_max_tokens(config, entry(), "high", ceiling=ceiling)

    seconds_needed = budget / rate
    assert seconds_needed <= config.stall_timeout, (
        f"{budget} tokens at {rate} tok/s needs {seconds_needed:.0f}s against a "
        f"{config.stall_timeout}s deadline: the model is still being given a budget the "
        "clock cannot pay for"
    )
    assert budget < config.thinking_max_tokens_floor


def test_the_ceiling_binds_an_explicit_budget_too() -> None:
    """The floor spares an explicit argument; the ceiling does not, and deliberately.

    A caller's number is a preference about how much room reasoning gets, and the floor
    respects it. The ceiling is a statement about what the clock can deliver, and a budget
    above it does not buy a longer answer -- it buys the same answer discarded at the
    deadline.
    """
    config = cfg()
    ceiling = DecodeRate(SOLO_TOKENS_PER_SECOND).ceiling(config, config.stall_timeout)

    assert resolve_max_tokens(config, entry(), "low", 131072, ceiling=ceiling) == ceiling
    # Still not *raised* to it. Below the ceiling the caller's number is untouched.
    assert resolve_max_tokens(config, entry(), "low", 500, ceiling=ceiling) == 500


def test_an_endpoint_that_publishes_no_rate_caps_nothing() -> None:
    """Degradation is the pre-ADR-0055 behaviour, not a guessed cap.

    A metrics surface can be absent, and inventing a rate to cap against would be the
    constant this design exists to avoid.
    """
    assert DecodeRate().known is False
    assert DecodeRate().ceiling(cfg(), 2100) is None
    assert DecodeRate(0).ceiling(cfg(), 2100) is None
    assert DecodeRate(SOLO_TOKENS_PER_SECOND).ceiling(cfg(), 0) is None


def test_the_floor_stops_a_slow_cluster_shrinking_the_budget_to_nothing() -> None:
    """ADR-0014 from the other side: a budget too small returns nothing just as surely."""
    config = cfg()
    crawling = DecodeRate(0.01)
    assert crawling.ceiling(config, config.stall_timeout) == config.reply_budget_floor


def test_an_implausible_turn_does_not_move_the_estimate() -> None:
    """A turn answering in a few tokens measures the queue, not the decoder."""
    rate = DecodeRate(SOLO_TOKENS_PER_SECOND)
    rate.observe(output_tokens=4, seconds=0.1)          # too few tokens
    rate.observe(output_tokens=5000, seconds=0.01)      # too short an interval
    assert rate.ceiling(cfg(), 2100) == DecodeRate(SOLO_TOKENS_PER_SECOND).ceiling(cfg(), 2100)


def test_the_estimate_follows_a_real_change_without_chasing_one_turn() -> None:
    """Our own turns replace the cluster's lifetime mean, but not all at once."""
    rate = DecodeRate(SOLO_TOKENS_PER_SECOND)
    one = cfg()

    rate.observe(output_tokens=2000, seconds=200.0)  # 10 tok/s, a much slower reality
    after_one = rate.ceiling(one, one.stall_timeout)
    assert after_one is not None
    assert after_one < DecodeRate(SOLO_TOKENS_PER_SECOND).ceiling(one, one.stall_timeout)
    assert after_one > DecodeRate(10.0).ceiling(one, one.stall_timeout)

    for _ in range(10):
        rate.observe(output_tokens=2000, seconds=200.0)
    settled = rate.ceiling(one, one.stall_timeout)
    assert abs(settled - DecodeRate(10.0).ceiling(one, one.stall_timeout)) < settled * 0.05


# --- the enlarged retry, which is where a partial fix would leak ------------------------

class _RecordingBackend:
    """Answers empty-at-length forever and records every budget it was asked for.

    Empty at a length stop is ADR-0014's exhaustion signature, so this drives
    `dispatch_with_recovery` through all three of its stages in one call -- which is the
    only way to see the budget the *retry* asks for.
    """

    def __init__(self) -> None:
        self.budgets: list[int] = []

    async def complete(self, request):
        self.budgets.append(request.max_tokens)
        return CanonicalResponse(
            content=(), finish_reason="length", input_tokens=10, output_tokens=0,
            model="served-id-1",
        )

    async def probe(self):
        return ("served-id-1",)

    async def probe_cluster(self):
        return {"decode_tokens_per_second_since_boot": SOLO_TOKENS_PER_SECOND}


def test_the_enlarged_retry_cannot_escape_the_ceiling() -> None:
    """Stage 2 doubles the budget, and `thinking_max_tokens_floor` is its own floor.

    Bounding only the first dispatch would leave that the way back to a budget the clock
    cannot pay -- and stage 2 is reached precisely when the model has already demonstrated
    it will spend everything it is given.
    """
    config = cfg()
    backend = _RecordingBackend()
    ceiling = DecodeRate(SOLO_TOKENS_PER_SECOND).ceiling(config, config.stall_timeout)

    asyncio.run(
        loop.dispatch_with_recovery(
            config, entry(), backend,
            lambda level, budget: loop.build_one_shot_request(
                delegation=loop.Delegation("x"), effort=level, max_tokens=budget,
                temperature=1.0,
            ),
            effort="high", deadline=None, budget_ceiling=ceiling,
        )
    )

    assert backend.budgets, "the cascade never dispatched"
    over = [b for b in backend.budgets if b > ceiling]
    assert not over, (
        f"{len(over)} of {len(backend.budgets)} dispatches asked for more than the "
        f"{ceiling}-token ceiling: {over}. The retry is a way back to a budget no "
        "deadline can pay for."
    )
