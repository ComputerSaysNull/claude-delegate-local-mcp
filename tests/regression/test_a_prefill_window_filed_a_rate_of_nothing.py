"""A scrape window inside a prefill generates no tokens, and that is not a decode rate.

Sampling the cluster's counter on a ticker means a window can land anywhere, including
entirely inside a prompt being processed. `generation_tokens_total` does not move during a
prefill, so the window differences to zero -- while `requests_running` is nonzero, because
a request being prefilled is very much running. Filed as-is that is a rate of zero tokens
per second at a busy concurrency: the worst possible sample, in the bucket a busy call is
priced from.

Not a rare shape either. Prefill runs at about 1,060 tok/s on this deployment, so a
175,000-token prompt spends roughly 143 seconds generating nothing at all -- more than
fourteen consecutive windows at the default interval.

So a window whose generation delta is zero is dropped. And the drops are *counted*, because
both of this sampler's guards are invisible in the memory they decline to write to: a
sampler refusing every window looks exactly like a quiet cluster, and there is no way to
tell them apart after the fact without a number.

Every test here is a pin rather than a red: `RateSampler` is new, so the committed code has
no symbol to assert against and an AttributeError proves nothing. CLAUDE.md is explicit
about that distinction.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.loop import RateHistory, RateSampler

WINDOW_SECONDS = 10.0
BUSY_TOKENS = 1_200      # 120 tok/s aggregate, 20 tok/s each across six streams


def scrape(tokens: int, *, running: float = 6.0, seconds: float = WINDOW_SECONDS) -> dict:
    """One `probe_cluster` payload, shaped exactly as `_DecodeWindow` fills it."""
    return {
        "decode_tokens_window": tokens,
        "decode_window_seconds": seconds,
        "decode_tokens_per_second_window": round(tokens / seconds, 2),
        "requests_running": running,
    }


def sampler(history: RateHistory, *, every: float = WINDOW_SECONDS) -> RateSampler:
    async def nothing():
        return None

    return RateSampler(history, probe=nothing, busy=lambda: True, every=every)


def test_a_window_that_generated_nothing_is_not_recorded():
    """The guard. Zero tokens at six concurrent is a prefill, not a cluster at zero."""
    history = RateHistory()
    s = sampler(history)

    assert s.record(scrape(0)) is False
    assert history.expect(6) is None


def test_a_window_that_generated_something_still_is():
    """The other half, and without it the guard passes by recording nothing at all.

    A guard that quietly became "record nothing" would satisfy the test above and destroy
    the sampler, which is exactly the failure mode CLAUDE.md's negative-test rule exists
    for.
    """
    history = RateHistory()
    s = sampler(history)

    assert s.record(scrape(BUSY_TOKENS)) is True
    assert history.expect(6, trusted=True) == pytest.approx(
        BUSY_TOKENS / WINDOW_SECONDS / 6
    )


def test_the_dropped_window_is_counted():
    """Visible rather than silent. The drop rate is the only sign the guard is firing."""
    history = RateHistory()
    s = sampler(history)

    for _ in range(3):
        s.record(scrape(0))
    s.record(scrape(BUSY_TOKENS))

    status = s.status()
    assert status["windows_dropped_prefill"] == 3
    assert status["samples_filed"] == 1
    assert status["windows_seen"] == 4


def test_a_slow_window_is_not_mistaken_for_a_prefill():
    """The control a guard like this usually breaks: pessimism is the point of the memory.

    A genuinely slow window -- a handful of tokens, but tokens -- has to survive. A guard
    that widened into "ignore bad news" would pass every test above and quietly turn the
    estimator optimistic, which is the error that kills a turn outright.
    """
    history = RateHistory()
    s = sampler(history)

    assert s.record(scrape(6)) is True
    assert history.expect(6, trusted=True) == pytest.approx(0.1)


def test_a_window_spanning_an_idle_stretch_is_dropped_and_counted():
    """The sampler ticks only while something is in flight, so windows have gaps.

    The first scrape of a busy period differences against the last scrape of the previous
    one, which may be hours old. That window reports the new burst's tokens spread over
    all the idle time as well -- a spuriously low rate at a genuinely busy concurrency,
    which is the same defect the prefill guard exists to stop, arriving by another route.
    Counted separately, because "the cluster was idle" and "the cluster was prefilling"
    are different things for an operator to read.
    """
    history = RateHistory()
    s = sampler(history)

    assert s.record(scrape(BUSY_TOKENS, seconds=3_600.0)) is False
    assert history.expect(6) is None
    assert s.status()["windows_dropped_stale"] == 1
    assert s.status()["windows_dropped_prefill"] == 0


def test_an_idle_scrape_is_not_a_drop():
    """Nothing running is nothing to divide by, and nothing measured to have thrown away.

    Counting it would make the drop rate read as a fault during exactly the periods when
    there is no work, which is when an operator is most likely to be looking.
    """
    history = RateHistory()
    s = sampler(history)

    assert s.record(scrape(0, running=0.0)) is False
    assert s.status() == {
        "sample_interval_seconds": WINDOW_SECONDS,
        "windows_seen": 0,
        "samples_filed": 0,
        "windows_dropped_prefill": 0,
        "windows_dropped_stale": 0,
    }


def test_a_scrape_with_no_window_yet_is_not_a_drop():
    """`_DecodeWindow` reports nothing until it has two scrapes to difference.

    That is the correct refusal of a rate that cannot be computed, not a measurement
    lost, so it must not show up as this sampler having discarded something.
    """
    history = RateHistory()
    s = sampler(history)

    assert s.record({"requests_running": 6.0}) is False
    assert s.record(None) is False
    assert s.status()["windows_seen"] == 0
