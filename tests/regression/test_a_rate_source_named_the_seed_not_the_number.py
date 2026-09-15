"""`rate_source` said where the *seed* came from, long after the seed stopped being the rate.

It is assigned once in `DecodeRate.__init__` and never again. `observe` then moves `_rate`
by an exponential average on every measurable turn, so from the second turn onward the
number beside the label is this delegation's own measurement while the label still names the
rate memory or the cluster's lifetime mean.

That is not cosmetic. `rate_source` is the field a reader uses to decide whether the number
can be trusted, and `ARCHITECTURE.md` already leans on it for a second fact -- that
`requests_running` is only a real cluster reading on a `cluster_since_boot` row. A label that
outlives what produced it makes both readings wrong, and it is in the hand-off notebook as a
transcript-reading trap, which is the sign it has already cost someone an hour.

The number and the label now change together, which is the only invariant worth having here:
a sample that is refused moves neither, and a sample that is taken moves both.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local.loop import DecodeRate

# Comfortably past `MIN_TOKENS` and `MIN_SECONDS`, so the sample is taken.
GOOD_TURN = (4000, 100.0)


def test_a_taken_sample_relabels_the_rate_as_this_delegation_s_own():
    """The bug. Today this still reads `observed_at_concurrency`."""
    r = DecodeRate(20.0, 3.0, source="observed_at_concurrency")

    r.observe(*GOOD_TURN)

    assert r.source == "own_turns"


def test_the_number_really_did_move():
    """Control on the premise. If the seed still stood, relabelling would be the bug."""
    r = DecodeRate(20.0, 3.0, source="observed_at_concurrency")

    r.observe(*GOOD_TURN)

    assert r.rate != 20.0


def test_a_refused_sample_moves_neither_the_number_nor_the_label():
    """The invariant, and the control that stops the fix becoming "always relabel".

    A turn below the floors says nothing about throughput. It leaves `_rate` alone, so it
    must leave the label alone too -- otherwise the record claims a measurement that was
    explicitly rejected.
    """
    r = DecodeRate(20.0, 3.0, source="observed_at_concurrency")

    r.observe(4, 0.01)

    assert r.rate == 20.0
    assert r.source == "observed_at_concurrency"


def test_an_unseeded_estimator_is_relabelled_by_its_first_sample():
    """Control. `unknown` is a real state and the first measurement has to leave it."""
    r = DecodeRate()
    assert r.source == "unknown"

    r.observe(*GOOD_TURN)

    assert r.source == "own_turns"
    assert r.known


def test_the_seed_keeps_its_label_until_a_sample_lands():
    """Control. Before any turn, the label is the honest answer and must not change."""
    r = DecodeRate(20.0, 3.0, source="cluster_since_boot")

    assert r.source == "cluster_since_boot"
    assert r.rate == 20.0
