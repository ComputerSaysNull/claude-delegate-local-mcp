"""One deque shared by every concurrency, so quiet samples evicted the busy ones.

`RateHistory` kept a single `deque(maxlen=64)` holding `(concurrency, rate)` pairs, evicted
by recency. Pricing widens to the busiest readings it has, so those carry all of the value:
a six-way reading is the one that stops a six-way delegation being priced for an idle
cluster.

Recency does not know that. Thirteen five-wide dispatches are sixty-five samples, and the
sixty-fifth pushes out the six-way reading that was the only thing standing between a burst
and a budget it cannot decode. The memory is then *worse* the more it is used, which is the
opposite of what a memory is for -- and it degrades silently, because a missing bucket falls
through to the widening and returns a plausible number from the wrong regime.

The fix is per-concurrency buckets, each retaining its own samples, so a flood at one
concurrency cannot displace what was learned at another. Eviction by value was the
alternative and is not taken: it would keep whichever sample the statistic favours for ever,
and the memory would stop tracking hardware that changed.

The cap survives per bucket rather than being dropped. Unbounded would be a slow leak in a
process that runs for days, and would let one ancient sample pin a bucket for the life of the
server -- which is the reason the original cap exists and is not weakened by moving it.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.loop import RateHistory


def observe(h: RateHistory, concurrency: int, rate: float) -> None:
    """Drive the public entry point, so the admission floors are exercised too."""
    h.observe(int(rate * 100), 100.0, concurrency=concurrency)


def test_a_flood_of_quiet_samples_does_not_evict_a_busy_one():
    """The fix. A six-way reading is what stops a six-way call being priced as idle.

    Against one shared deque the solo flood walks it out and `expect(6)` falls through to
    the widening, which has nothing busy left to offer either.
    """
    h = RateHistory(keep=8)
    observe(h, 6, 18.5)
    for _ in range(40):
        observe(h, 1, 44.0)

    assert h.expect(6) == 18.5, "the busy sample was evicted by quiet ones"


def test_a_flood_at_one_concurrency_does_not_evict_another_flood():
    """The same failure between two busy buckets, which recency cannot tell apart."""
    h = RateHistory(keep=8)
    for _ in range(12):
        observe(h, 4, 26.0)
    for _ in range(40):
        observe(h, 2, 30.0)

    assert h.expect(4) == 26.0, "the four-way bucket was walked out by two-way samples"


def test_each_bucket_still_forgets_its_own_oldest():
    """Control, and the reason the cap moves rather than goes.

    Per-bucket retention must still bound the memory, or a long-lived process accumulates
    for ever and an ancient sample pins the estimate. The newest value has to win.
    """
    h = RateHistory(keep=4)
    observe(h, 3, 9.0)
    for _ in range(4):
        observe(h, 3, 25.0)

    assert h.expect(3) == 25.0, "an evicted sample is still answering"


def test_the_median_within_a_bucket_is_what_is_returned():
    """Rewritten rather than deleted: it asserted the minimum, which was a second defect.

    The bucketing this file is about is untouched -- what changed is the statistic read
    off a bucket. Three samples at one width are one regime measured three times, so the
    spread between them is noise and the worst draw measures nothing; measured
    2026-09-20, the mean tracked the operator benchmark to 1-3% where the minimum sat
    24-71% low. The asymmetry that made a minimum attractive is still paid, one level up:
    the widening below takes the worst bucket *median*.
    """
    h = RateHistory(keep=8)
    for rate in (30.0, 22.5, 27.1):
        observe(h, 2, rate)

    assert h.expect(2, trusted=True) == pytest.approx(27.1)


def test_the_widening_still_answers_an_empty_bucket():
    """Control. Nothing seen at four, so a busier sample must still bound it from below."""
    h = RateHistory(keep=8)
    observe(h, 1, 44.1)
    observe(h, 6, 10.95)

    assert h.expect(4, trusted=True) == 10.95
    assert h.expect(6, trusted=True) == 10.95


def test_a_quieter_sample_never_answers_a_busier_question():
    """Control. A solo measurement says nothing about six, and must not be offered."""
    h = RateHistory(keep=8)
    observe(h, 1, 44.1)

    assert h.expect(6, trusted=True) is None


def test_the_memory_survives_a_round_trip_through_its_file(tmp_path):
    """Control. Bucketing must not change what persistence preserves.

    The file is the reason the memory is warm after a reconnect, so a shape change that
    silently stopped restoring the busy samples would reintroduce the cold start.
    """
    p = tmp_path / "rate-history.json"
    a = RateHistory(keep=8, path=p, stamp="model-x")
    observe(a, 6, 18.5)
    observe(a, 1, 44.0)

    b = RateHistory(keep=8, path=p, stamp="model-x")

    assert b.expect(6) == 18.5
    assert b.expect(1, trusted=True) == 44.0
