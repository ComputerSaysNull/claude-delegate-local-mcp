"""A delegation priced itself 95,612 tokens at 88.53 tok/s on a cluster that decodes 44.1.

ADR-0071 raised `RateHistory`'s floor to 512 tokens after a 237-token turn recorded
**16.64 tok/s** -- far too *low* -- and deliberately left `DecodeRate`'s floor at 64, on the
reasoning that an average which decays needs a weaker admission test than a minimum which
does not. That was measured against the pre-#172 instrument, where the denominator still
contained prefill and queueing, so a short turn read slow.

#172 (ADR-0070) changed the denominator to `decode_seconds` -- last token minus first.
**The sign inverted.** A short turn's frames arrive in one tight burst, the interval is
barely over `MIN_SECONDS`, and the same shape of sample that used to read 2.6x too low now
reads over 2x too high. An average that decays is no protection when nearly every turn is
short: the estimate climbs monotonically instead of wandering.

Measured 2026-09-13 on this deployment, recovering each turn's own sample from the next
turn's `priced` event by inverting the EMA at `WEIGHT = 0.4`. The dose-response is exact:

    105-119 output tokens, 1.00-1.30s decode  ->   91.3, 102.1, 104.8, 105.9 tok/s
    438-1478 output tokens, 8.6-31.2s decode  ->   45.3,  47.4,  50.3,  51.1,  52.5 tok/s

The owner's benchmark for this cluster is **44.1 tok/s solo**. The large-turn samples agree
with it; every sample above 85 came from a turn of about a hundred tokens. Across 24 such
turns the estimate climbed 19.57 -> 88.53 and never once fell back, authorising 95,612
tokens for a turn the clock can pay about 20,000 of.

The fix is that both estimators consume the *same measured quantity* now, so the test for
whether a sample is a measurement at all belongs to the measurement rather than to the
consumer. Average-versus-minimum decides how a bad sample propagates, not whether it is one.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.loop import DecodeRate, RateHistory

# What the cluster actually does, from the owner's benchmark (JOURNAL 2026-09-12).
SOLO_BENCHMARK = 44.1

# Tokens and the decode interval, never a rate -- the quantity the estimator is handed.
TINY_TURN = (105, 1.002)        # recovered at 104.81 tok/s, 2.4x the solo benchmark
SMALL_TURN = (119, 1.304)       # recovered at  91.25 tok/s
HONEST_TURN = (453, 9.004)      # recovered at  50.31 tok/s -- a real measurement
SLOW_HONEST_TURN = (1478, 31.205)  # recovered at 47.36 tok/s, the largest turn seen

# Dispatch 0010's 24 turns as recovered, in order. The seed is its own first priced rate.
SEED = 19.57
OBSERVED_RUN = [
    (63, 3.220), (142, 2.307), (96, 2.090), (184, 2.784), (185, 3.227), (157, 2.893),
    (257, 4.336), (119, 1.304), (182, 2.908), (159, 2.329), (267, 4.224), (453, 9.004),
    (439, 8.595), (206, 2.419), (105, 1.002), (169, 2.547), (208, 2.785), (237, 3.057),
    (148, 2.049), (438, 9.678), (107, 1.010), (130, 1.912), (108, 1.258), (105, 1.029),
]


def test_a_tiny_turn_is_not_a_decode_measurement():
    """The bug, at its smallest. 105 tokens in a second describes the burst, not the decoder."""
    r = DecodeRate(seed=SEED, source="observed_at_concurrency")
    r.observe(*TINY_TURN)
    assert r.rate == pytest.approx(SEED)


def test_the_estimate_never_climbs_past_what_the_cluster_can_decode():
    """The bug as it was actually met: 24 turns, and the climb never reversed.

    This is the assertion that fails loudest against the unfixed code, which finishes at
    88.53 -- twice the benchmark, and the number that authorised 95,612 tokens.
    """
    r = DecodeRate(seed=SEED, source="observed_at_concurrency")
    for tokens, seconds in OBSERVED_RUN:
        r.observe(tokens, seconds)
    assert r.rate is not None
    assert r.rate <= SOLO_BENCHMARK


def test_the_two_estimators_now_share_one_admission_test():
    """Supersedes `test_the_guard_is_stricter_than_the_within_delegation_one`.

    That test asserted `DecodeRate.MIN_TOKENS < 237 < RateHistory.MIN_TOKENS` and its
    docstring defended the gap. The gap was right while the two divided by different
    things; after #172 they divide by the same `decode_seconds`, so a sample either
    describes the decoder or it does not, whoever is asking.
    """
    assert DecodeRate.MIN_TOKENS == RateHistory.MIN_TOKENS


def test_an_honest_turn_is_still_learned_from():
    """The control that matters, and the one a guard like this usually breaks.

    A floor that quietly became "never learn anything" would pass both tests above and
    destroy the estimator, leaving every delegation pinned to its seed for ever.
    """
    r = DecodeRate(seed=SEED, source="observed_at_concurrency")
    r.observe(*SLOW_HONEST_TURN)
    assert r.rate is not None
    assert r.rate > SEED


def test_a_genuinely_slow_long_turn_still_lowers_the_estimate():
    """Pessimism survives the guard: this must not become "ignore bad news"."""
    r = DecodeRate(seed=SOLO_BENCHMARK, source="cluster_since_boot")
    r.observe(13268, 1750.0)   # 7.58 tok/s, the PLAN sample that does not fit one turn
    assert r.rate is not None
    assert r.rate < SOLO_BENCHMARK


def test_the_floor_the_run_would_now_learn_nothing_from():
    """States the cost of the fix rather than hiding it.

    Dispatch 0010's largest turn was 453 tokens, so under the shared floor that entire
    44-minute run teaches the estimator nothing and it holds its seed. That is the
    conservative direction -- an under-authorised turn is short of budget, never killed --
    and it is why persisting `RateHistory` across a reconnect is the item that follows this
    one rather than the one that preceded it.
    """
    assert max(tokens for tokens, _ in OBSERVED_RUN) < DecodeRate.MIN_TOKENS
