"""`expect` priced from a bucket's minimum, so the worst minute it ever saw was the rate.

The minimum was chosen as protection: the error is asymmetric, and a budget that cannot be
decoded inside `turn_timeout` kills the turn with nothing where an under-estimate merely
truncates. The protection is sound *between* buckets, because contention only slows a
stream and a busier measurement really does bound a quieter question from below. Inside one
bucket it was not protection at all, it was a sampling artefact: every sample there was
taken at the same width, so the spread between them is noise, and the minimum of 64 draws
from that noise is not a measurement of anything.

Measured 2026-09-20. At every well-populated concurrency the bucket *mean* matched the
operator benchmark to within 1-3%, while the bucket *minimum* sat 24-71% below it. And the
minimum gets worse the longer the server runs: the more moments a bucket holds, the likelier
one of them is the worst minute the cluster had, so a memory that is supposed to improve
with use degrades with it.

That under-pricing is not free. Measured 2026-09-22, 31 of 953 recorded turns ended with
`output_tokens` exactly equal to `budget_ceiling` -- the budget genuinely binds -- and every
one of the 31 was a large answer. Pricing low truncates the biggest replies the server
produces.

The fix keeps the structure and changes only the statistic. A bucket answers with its mean;
the widening to busier buckets takes the worst of those means. Pooling every sample from
every busier bucket into one mean is the obvious alternative and is wrong -- it mixes
regimes, so a quiet bucket's fast samples would pull a six-way answer up past anything
six-way was ever measured at.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.loop import RateHistory

# The six-way regime as this cluster actually behaves: a benchmark near 19.4 tok/s with
# real spread around it. Deliberately not a series whose mean and minimum coincide -- such
# a series cannot tell the two statistics apart, so a test built on one cannot fail.
SIX_WAY = (19.4, 12.0, 20.6, 24.4)     # mean 19.1, minimum 12.0
SIX_WAY_MEAN = sum(SIX_WAY) / len(SIX_WAY)

# A quieter regime, with its own spread. Its mean and its minimum are both far above the
# six-way bucket's, which is what makes the widening's direction visible.
SOLO = (44.1, 30.0)                    # mean 37.05, minimum 30.0
SOLO_MEAN = sum(SOLO) / len(SOLO)


def fill(h: RateHistory, concurrency: int, rates) -> None:
    """Drive the public entry point, so the admission floors are exercised too.

    A thousand-second interval rather than a hundred, so that even the slowest rate here
    clears `MIN_TOKENS`. At a hundred, the deliberately bad sample below is 500 tokens and
    is refused -- which would leave the bucket clean and the test passing for the wrong
    reason.
    """
    for rate in rates:
        h.observe(int(rate * 1000), 1000.0, concurrency=concurrency)


def test_a_bucket_answers_with_its_mean_not_its_minimum():
    """The bug. Four six-way moments average 19.1 tok/s; the worst of them says 12.0.

    12.0 is a minute the cluster had, not the rate it decodes at, and it priced every
    six-way turn for as long as it stayed in the bucket.
    """
    h = RateHistory()
    fill(h, 6, SIX_WAY)

    assert h.expect(6, trusted=True) == pytest.approx(SIX_WAY_MEAN)


def test_the_untrusted_question_also_gets_the_mean():
    """The same bucket reached through the widening rather than directly.

    Both routes had their own `min()`, so fixing one and not the other would leave every
    delegation that cannot trust its concurrency label -- which is every one of them
    unless `admission_idle_hold` is on -- still priced from the worst minute.
    """
    h = RateHistory()
    fill(h, 6, SIX_WAY)

    assert h.expect(6) == pytest.approx(SIX_WAY_MEAN)


def test_the_widening_takes_the_worst_bucket_mean_not_the_worst_sample():
    """The part of the pessimism that survives, and the part that does not.

    A solo question widens over every busier bucket, so it is answered by six-way. What
    it must be answered with is that bucket's *mean*, 19.1 -- not the worst single sample
    in it, 12.0, which is what the committed code returns.
    """
    h = RateHistory()
    fill(h, 1, SOLO)
    fill(h, 6, SIX_WAY)

    assert h.expect(1) == pytest.approx(SIX_WAY_MEAN)


def test_a_quiet_buckets_fast_samples_never_answer_a_busy_question():
    """The control that rules out the obvious wrong fix, which is one pooled mean.

    Pooled, the solo samples would drag a six-way answer to about 25 tok/s -- above
    anything six-way was ever measured at, and an over-estimate is the error that kills a
    turn outright. Taking the minimum *of the means* keeps the direction that is sound.
    """
    h = RateHistory()
    fill(h, 1, SOLO)
    fill(h, 6, SIX_WAY)

    pooled = sum(SOLO + SIX_WAY) / len(SOLO + SIX_WAY)
    assert h.expect(6) == pytest.approx(SIX_WAY_MEAN)
    assert h.expect(6) < pooled


def test_a_quieter_sample_still_never_answers_a_busier_question():
    """Control. A solo measurement says nothing about six-way and must not be offered."""
    h = RateHistory()
    fill(h, 1, SOLO)

    assert h.expect(6) is None
    assert h.expect(1, trusted=True) == pytest.approx(SOLO_MEAN)


def test_the_estimate_stops_getting_worse_as_the_bucket_fills():
    """The mechanism, stated as the property it breaks.

    A minimum over 64 draws is a worse estimate than a minimum over four, because the
    extra draws can only lower it. That makes the memory degrade with use -- the opposite
    of what a memory is for. A mean does not move with the sample count, only with the
    rate, so the same regime measured longer answers the same.
    """
    short = RateHistory()
    fill(short, 6, SIX_WAY)

    long = RateHistory()
    for _ in range(16):
        fill(long, 6, SIX_WAY)

    assert long.expect(6) == pytest.approx(short.expect(6))
    # Asserted against the value as well as against each other. Two minima are also equal
    # to each other here, so the comparison alone would pass against the committed code
    # and prove nothing.
    assert long.expect(6) == pytest.approx(SIX_WAY_MEAN)


def test_one_bad_minute_no_longer_prices_a_full_bucket():
    """The headline. Sixty-three good moments and one bad one is not a slow cluster."""
    h = RateHistory(keep=64)
    fill(h, 6, [19.4] * 63)
    fill(h, 6, [5.0])

    assert h.expect(6, trusted=True) == pytest.approx(19.175, rel=1e-3)
