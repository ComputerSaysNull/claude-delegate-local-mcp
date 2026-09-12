"""Every delegation priced its reply against a cluster that was about to stop being idle.

`seed_decode_rate` reads the endpoint's since-boot mean, and the loop then prices the first
turn from it. Measured 2026-09-11: six delegations fanned out in one message each read
`requests_running` of 0 or 1 and were all given the same ~44,111 token ceiling at 35.0
tok/s -- because admission serialises a fan-out, so at the instant any of them prices, the
contention it is about to meet does not exist yet. Prose decodes at 19.4 tok/s at six
concurrent. The budget is spent in a world the pricing never saw.

No dispatch-time reading of the *cluster* can fix that. Admission can: a lease is taken
before the request is issued, so the gate knows about a sibling that `num_requests_running`
cannot see yet, and it knows about the ones still queued behind it, which will contend as
soon as they are released.

`RateHistory` is the other half. The rate for a given concurrency is not modelled, because
a model would be a constant baked to one deployment's hardware -- the mistake PLAN.md
records against `kv_token_budget`. It is remembered: every completed turn reports what it
achieved and how contended it was, and pricing asks for the worst seen at that
concurrency or above.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

from claude_delegate_local.loop import RateHistory, seed_decode_rate


class Cluster:
    """A backend whose only job is to report a since-boot mean, and say if it was asked."""

    def __init__(self, rate: float | None = 35.0) -> None:
        self.rate, self.probed = rate, 0

    async def probe_cluster(self):
        self.probed += 1
        if self.rate is None:
            return None
        return {"decode_tokens_per_second_since_boot": self.rate, "requests_running": 0.0}


def seed(backend, history=None, expected=1):
    return asyncio.run(seed_decode_rate(backend, history, expected))


def test_a_remembered_rate_beats_the_clusters_since_boot_blend():
    """The behaviour change. 19 tok/s seen at six concurrent, not 35 averaged over all."""
    h = RateHistory()
    h.observe(1900, 100.0, concurrency=6)
    rate = seed(Cluster(35.0), h, expected=6)
    assert rate.rate == 19.0
    assert rate.source == "observed_at_concurrency"


def test_the_cluster_is_not_even_asked_when_memory_answers():
    """A scrape per dispatch is a real cost, and the answer was already known.

    Asserted because "prefers the memory" and "reads both and picks" are the same from
    outside, and only the first is what was intended.
    """
    h = RateHistory()
    h.observe(1900, 100.0, concurrency=6)
    backend = Cluster(35.0)
    seed(backend, h, expected=6)
    assert backend.probed == 0


def test_an_empty_memory_falls_through_to_the_cluster():
    """Cold start keeps the old behaviour rather than inventing a pessimistic constant."""
    backend = Cluster(35.0)
    rate = seed(backend, RateHistory(), expected=6)
    assert rate.rate == 35.0
    assert rate.source == "cluster_since_boot"
    assert backend.probed == 1


def test_a_cluster_that_publishes_nothing_still_caps_nothing():
    """The pre-ADR-0055 behaviour, and it must survive both new code paths."""
    rate = seed(Cluster(None), RateHistory(), expected=4)
    assert rate.rate is None
    assert rate.source == "unknown"


def test_nothing_observed_yet_is_not_a_rate():
    """Cold start has no answer, and inventing one is what this exists to avoid.

    `None` sends the caller back to the cluster's since-boot mean, which is the behaviour
    that preceded this and is merely optimistic rather than wrong.
    """
    assert RateHistory().expect(4) is None


def test_the_worst_seen_at_that_concurrency_is_what_is_returned():
    """Pessimism is the point: the budget must survive the bad case, not the mean one."""
    h = RateHistory()
    h.observe(3000, 100.0, concurrency=4)
    h.observe(1900, 100.0, concurrency=4)
    h.observe(2500, 100.0, concurrency=4)
    assert h.expect(4) == 19.0


def test_a_busier_observation_counts_for_a_quieter_question():
    """A rate seen at six concurrent is a safe answer for four, never the reverse.

    Contention only slows a stream, so a worse-contended measurement bounds a
    better-contended one from below. Ignoring them would discard the observations that
    matter most, which are the ones taken while the cluster was busy.
    """
    h = RateHistory()
    h.observe(1900, 100.0, concurrency=6)
    assert h.expect(4) == 19.0


def test_a_quieter_observation_does_not_answer_a_busier_question():
    """The direction that would be unsafe. A solo rate says nothing about six-way."""
    h = RateHistory()
    h.observe(4400, 100.0, concurrency=1)
    assert h.expect(6) is None


def test_observations_do_not_accumulate_without_bound():
    """A process runs for days; an unbounded list is a slow leak and a stale memory.

    The bound also stops one ancient bad sample pinning the estimate forever.
    """
    h = RateHistory(keep=3)
    for n in range(10):
        h.observe((n + 1) * 1000, 1000.0, concurrency=2)
    assert h.expect(2) == 8.0, "the oldest observations should have fallen off"


def test_an_implausible_observation_is_refused():
    """Zero and negative are arithmetic accidents, not measurements.

    `DecodeRate` already refuses implausible observations for the same reason; letting one
    in here would price every later delegation against it. Asserted on the inputs rather
    than on the quotient: the two floors make a non-positive rate unreachable, so a guard
    on the division would be a check that can never fire.
    """
    h = RateHistory()
    h.observe(0, 100.0, concurrency=2)
    h.observe(5000, 0.0, concurrency=2)
    h.observe(-5000, 100.0, concurrency=2)
    h.observe(5000, -100.0, concurrency=2)
    assert h.expect(2) is None
