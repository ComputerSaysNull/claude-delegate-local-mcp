"""The rate memory was filled per completed turn, so the wider the fan-out the less it held.

A sample was filed when a turn finished, by the turn. Six streams finishing therefore put
six samples on one moment, and `RateHistory`'s bucket holds `DEFAULT_KEEP` samples however
they were produced -- so bucket 6 spanned roughly 10.7 moments where bucket 2 spanned 32.
The bucket a busy call is priced from was the one with the shortest memory, and two
buckets' figures were not comparable with each other because they covered different
amounts of wall-clock time.

Worse, the concurrency each sample was filed under came from `expected_concurrency`, frozen
when the admission lease was granted. That is a prediction made before the turn ran, so a
sample carried whatever contention that one turn happened to be *expected* to meet rather
than what the cluster was actually doing while the tokens came out.

The fix is to sample the cluster's own counter on a ticker instead. `_DecodeWindow` already
differences `generation_tokens_total` across two scrapes, so one scrape yields the tokens,
the seconds and `requests_running` together -- one sample per moment, the rate and its
divisor read at the same instant, and every bucket spanning the same span of wall clock at
every width.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest
from test_loop import FakeClock, cfg, entry

from claude_delegate_local import loop
from claude_delegate_local.backends.base import CanonicalResponse, TextBlock

# A turn big enough and slow enough to clear both of `RateHistory.observe`'s floors, so
# that a sample failing to appear is the behaviour under test rather than an admission
# refusal quietly doing the same thing.
OUTPUT_TOKENS = 2_000
DECODE_SECONDS = 100.0

# One six-wide moment, as the scrape reports it: the aggregate tokens differenced over the
# window, the window itself, and the concurrency read in the same scrape.
WINDOW_SECONDS = 10.0
SIX_WIDE_TOKENS = 1_200          # 120 tok/s aggregate, 20 tok/s per stream


def scrape(tokens: int, *, running: float, seconds: float = WINDOW_SECONDS) -> dict:
    """One `probe_cluster` payload, shaped exactly as `_DecodeWindow` fills it."""
    return {
        "decode_tokens_window": tokens,
        "decode_window_seconds": seconds,
        "decode_tokens_per_second_window": round(tokens / seconds, 2),
        "requests_running": running,
    }


class Answering:
    """One turn that decodes a real number of tokens over a real interval."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    async def complete(self, request, *, on_token=None):
        self.clock.advance(DECODE_SECONDS)
        return CanonicalResponse(
            content=(TextBlock("an answer"),),
            finish_reason="stop",
            input_tokens=100,
            output_tokens=OUTPUT_TOKENS,
            model="served-id-1",
            decode_seconds=DECODE_SECONDS,
        )

    async def probe_cluster(self):
        """Nothing published, so only the code under test can put anything in the memory."""
        return None


def run_one_turn(history, config=None):
    clock = FakeClock()
    return asyncio.run(
        loop.run_agentic_loop(
            config or cfg(),
            entry(),
            Answering(clock),
            loop.Delegation("summarise this"),
            allowed=frozenset(),
            max_turns=2,
            clock=clock,
            rate_history=history,
            expected_concurrency=6,
        )
    )


def sampler(history, *, probe=None, busy=lambda: True, every=WINDOW_SECONDS):
    async def nothing():
        return None

    return loop.RateSampler(
        history, probe=probe or nothing, busy=busy, every=every
    )


def test_a_completed_turn_no_longer_files_a_sample_of_its_own():
    """The behaviour change, and the half of it that is a red against the committed code.

    A turn filing its own sample is what put six readings on one moment. With the sampler
    running it must file none: two feeders into one bucket would leave the widths
    incomparable exactly as before, and the per-turn one is the feeder whose count depends
    on the fan-out.
    """
    history = loop.RateHistory()
    run_one_turn(history)

    assert history.expect(6) is None, (
        "the completed turn filed a sample of its own, so six streams still put six "
        "readings on one moment"
    )


def test_turning_the_sampler_off_puts_the_per_turn_feed_back():
    """A pin, not a red: it names a setting the committed code does not have.

    A memory with no feeder at all is the cold start that costs every delegation's first
    turn, which is the thing the memory exists for. So disabling sampling has to restore
    the older feeder rather than leave the bucket empty.
    """
    history = loop.RateHistory()
    run_one_turn(history, cfg(rate_sample_seconds=0.0))

    assert history.expect(6) == pytest.approx(OUTPUT_TOKENS / DECODE_SECONDS)


def test_one_scrape_files_one_sample_whatever_the_width():
    """A pin: `RateSampler` is new. This is the property the whole item turns on.

    Six streams decoding is still one moment. The aggregate is divided by the concurrency
    read in the *same* scrape, so what lands is the per-stream rate at that width.
    """
    history = loop.RateHistory()
    s = sampler(history)

    assert s.record(scrape(SIX_WIDE_TOKENS, running=6.0)) is True
    assert history.expect(6, trusted=True) == pytest.approx(
        SIX_WIDE_TOKENS / WINDOW_SECONDS / 6
    )
    assert s.status()["samples_filed"] == 1


def test_sixty_four_six_wide_scrapes_leave_sixty_four_moments():
    """A pin: it can only be written against the sampler, which is new.

    The measurement the item is named for. Per completed turn, 64 slots at six-wide held
    about 10.7 moments; one sample per scrape makes it 64 at every width. Each moment
    carries a different rate, so the count of *distinct* values is the count of moments
    that survived rather than a count of writes.
    """
    history = loop.RateHistory(keep=loop.RateHistory.DEFAULT_KEEP)
    s = sampler(history)

    for moment in range(loop.RateHistory.DEFAULT_KEEP):
        # A different aggregate every moment, all well clear of zero so none is dropped
        # by the prefill guard.
        assert s.record(scrape(600 + moment * 6, running=6.0)) is True

    held = history.samples_at(6)
    assert len(set(held)) == loop.RateHistory.DEFAULT_KEEP, (
        f"bucket 6 spans {len(set(held))} moments, not {loop.RateHistory.DEFAULT_KEEP}"
    )

    # And solo holds the same number of moments, which is what makes the two buckets'
    # means comparable at all. Per completed turn this was six to one.
    solo = loop.RateHistory(keep=loop.RateHistory.DEFAULT_KEEP)
    solo_sampler = sampler(solo)
    for moment in range(loop.RateHistory.DEFAULT_KEEP):
        assert solo_sampler.record(scrape(400 + moment * 4, running=1.0)) is True

    assert len(solo.samples_at(1)) == len(held)


def test_the_ticker_does_not_scrape_an_idle_cluster():
    """A pin. An idle tick has no rate to sample and must reach no endpoint.

    Asserted because "samples while busy" and "samples always and records while busy" are
    the same from the memory's point of view, and only the first is intended: the second
    keeps a connection warm against a cluster this process is not using.
    """
    probes = 0

    async def probe():
        nonlocal probes
        probes += 1
        return scrape(SIX_WIDE_TOKENS, running=6.0)

    history = loop.RateHistory()
    s = sampler(history, probe=probe, busy=lambda: False, every=0.001)

    async def briefly():
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(briefly())

    assert probes == 0
    assert history.expect(6) is None


def test_a_disabled_sampler_never_starts():
    """A pin. Zero is how an operator turns sampling off, and it must cost no ticker."""
    s = sampler(loop.RateHistory(), every=0.0)

    assert s.enabled is False
    asyncio.run(asyncio.wait_for(s.run(), timeout=1.0))
