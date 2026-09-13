"""One 237-token turn priced every later delegation on this server at 3.9x too slow.

`DecodeRate.observe` refuses a sample from a turn too short to measure -- loop.py line 309
says why: *"A turn answering in a few tokens spends most of its backend interval on prefill
and queueing, so its apparent decode rate describes the queue rather than the decoder."*
`RateHistory.observe` never applied that test, and it is the one that matters: `DecodeRate`
dies with its delegation and keeps an exponential average that decays, while `RateHistory`
outlives every delegation and feeds a `min()` that is permanent for 64 observations. **A
minimum needs a stricter admission test than an average**, and it had a weaker one -- none.

Measured 2026-09-12 on this deployment. A turn emitting 237 tokens over a 14.2s decode
interval recorded 16.64 tok/s. The same model, the same day, reproducing the same file
alone, decoded 2,272 tokens at **65.6 tok/s**. `expect(1)` returns the worst sample ever
seen, so 16.64 became the floor: two separate fan-outs later, the first call admitted in
each was handed a ceiling of **17,969** tokens where the clock could pay for ~118,000.

The first-admitted member of every burst is the one that asks `expect(1)`, so the poisoned
minimum lands on it every time -- which is why this reads as "a burst's first member is
mispriced" in the roadmap, and why the fix is here rather than in the concurrency counter.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.loop import DecodeRate, RateHistory

# The 2026-09-12 samples, as measured. Tokens and the decode interval, not a rate --
# the whole defect is that the memory was handed a rate and could not judge it.
SHORT_TURN = (237, 14.244)        # 16.64 tok/s, and it described the queue
SOLO_TURN = (2272, 34.626)        # 65.6 tok/s, same model, same day, running alone
SIX_WAY_TURN = (2301, 83.194)     # 27.7 tok/s, six concurrent -- legitimately slow


def test_a_short_turn_is_not_remembered():
    """The bug. 237 tokens is not a measurement of throughput, and it outlived the run."""
    h = RateHistory()
    h.observe(*SHORT_TURN, concurrency=1)
    h.observe(*SOLO_TURN, concurrency=1)
    assert h.expect(1) == pytest.approx(65.6, rel=0.01)


def test_the_short_sample_is_refused_rather_than_outvoted():
    """Not "kept but rarely returned" -- `expect` takes a minimum, so it must never enter."""
    h = RateHistory()
    h.observe(*SHORT_TURN, concurrency=1)
    assert h.expect(1) is None


def test_a_legitimately_slow_long_turn_is_still_remembered():
    """The control that matters, and the one a guard like this usually breaks.

    Pessimism is the point of the minimum: a budget has to survive the bad case. A guard
    that quietly became "ignore bad news" would pass the test above and destroy the
    estimator, so a genuinely slow turn -- 13,268 tokens over 1,750s, 7.6 tok/s, the
    `PLAN.md` sample that does not fit one turn -- has to survive.
    """
    h = RateHistory()
    h.observe(13268, 1750.0, concurrency=6)
    assert h.expect(6) == pytest.approx(7.58, rel=0.01)


def test_the_minimum_still_answers_a_quiet_question_with_a_busy_measurement():
    """What must NOT change, and it is why the burst's first member needs no counter fix.

    `expect(1)` searching every sample and keeping the worst is the design, not the defect:
    contention only slows a stream, so a six-way measurement bounds a solo one from below.
    That is what already protects the first call admitted in a fan-out, which asks for
    concurrency 1 and gets the six-way floor.
    """
    h = RateHistory()
    h.observe(*SOLO_TURN, concurrency=1)
    h.observe(*SIX_WAY_TURN, concurrency=6)
    assert h.expect(1) == pytest.approx(27.7, rel=0.01)
    assert h.expect(6) == pytest.approx(27.7, rel=0.01)


def test_the_short_turn_no_longer_clears_either_guard():
    """Superseded 2026-09-13. This asserted the two floors were deliberately unequal.

    That was right while they divided by different things: with prefill inside the
    interval a short turn read *slow*, so only the permanent minimum needed protecting.
    Since ADR-0070 both divide by `decode_seconds` and the sign inverted -- a short turn
    now reads *fast* -- so the sample is refused by both, and the 237-token turn that named
    this file is below the shared floor rather than between two.

    See `test_a_small_turn_read_faster_than_the_cluster.py` for the measurement.
    """
    assert SHORT_TURN[0] < DecodeRate.MIN_TOKENS == RateHistory.MIN_TOKENS


def test_a_zero_interval_is_not_divided_by():
    """Degenerate rather than instantaneous, and it reached this code as a rate before."""
    h = RateHistory()
    h.observe(5000, 0.0, concurrency=1)
    assert h.expect(1) is None
