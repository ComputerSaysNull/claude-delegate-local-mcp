"""Remembering the decode rate switched off the reading of the KV pool.

`seed_decode_rate` returned as soon as the history had a rate for this concurrency, and the
`on_pool` report sits *after* that return. While the rate memory was per-process it was cold
on every reconnect, so the scrape always ran and the pool was always read. ADR-0075 made the
memory survive reconnects, and the scrape then stopped happening at all.

Measured 2026-09-14 on a freshly reconnected server, after nine delegations: every dispatch
priced `observed_at_concurrency`, `kv_cache_size_tokens_seen` was null, and
`kv_token_budget_effective` stood at 2,400,000 against a reported pool of 1,467,988 -- the
1.63x drift the pool reporting exists to prevent.

Each half passed its own tests throughout. The rate memory returned the right rate; the pool
reader reported the right pool when it ran. It was the *pairing* that broke, so that is what
these assert.
"""

from __future__ import annotations

import asyncio

from claude_delegate_local.loop import RateHistory, seed_decode_rate

POOL = 1_467_988
CONCURRENCY = 4


class _Cluster:
    """A backend that reports a pool and counts how often it is asked."""

    def __init__(self) -> None:
        self.scrapes = 0

    async def probe_cluster(self) -> dict:
        self.scrapes += 1
        return {
            "kv_cache_size_tokens": POOL,
            "decode_tokens_per_second_since_boot": 33.87,
            "requests_running": 0,
        }


def _warm() -> RateHistory:
    """A history that already answers for this concurrency, as it does after a reconnect."""
    history = RateHistory()
    history.observe(4096, 200.0, concurrency=CONCURRENCY)
    assert history.expect(CONCURRENCY) is not None, "the fixture must be warm"
    return history


def _seed(backend, history, on_pool):
    return asyncio.run(
        seed_decode_rate(backend, history, CONCURRENCY, on_pool=on_pool)
    )


def test_a_warm_memory_still_reports_the_pool_when_it_is_wanted():
    """The bug: the early return skipped the only sighting of the pool on this path."""
    backend, seen = _Cluster(), []

    rate = _seed(backend, _warm(), seen.append)

    assert seen == [POOL], "the pool was never reported"
    assert backend.scrapes == 1
    # And the remembered rate still wins the pricing question: a warm memory must not be
    # made worse than a cold one by the scrape it now performs.
    assert rate.source == "observed_at_concurrency"


def test_the_scrape_is_skipped_once_the_pool_is_known():
    """The control, and what keeps this one extra read per process rather than per call.

    The caller stops passing `on_pool` once the gate has the figure, and that is what lets
    the early return come back. Without this a fix for the test above would scrape on every
    delegation for ever.
    """
    backend = _Cluster()

    rate = _seed(backend, _warm(), None)

    assert backend.scrapes == 0, "it scraped when nobody wanted the pool"
    assert rate.source == "observed_at_concurrency"


def test_a_cold_memory_reports_the_pool_as_it_always_did():
    """The behaviour that was never broken, asserted so a fix cannot trade one for the other."""
    backend, seen = _Cluster(), []

    rate = _seed(backend, RateHistory(), seen.append)

    assert seen == [POOL]
    assert rate.source == "cluster_since_boot"


def test_a_failed_scrape_does_not_throw_away_a_remembered_rate():
    """The trade this fix must not make.

    Scraping for the pool introduces a failure that a warm memory did not previously have.
    A momentary `/metrics` outage must not turn a measured rate into `unknown` -- that would
    price the turn worse than before the fix, in exchange for a figure that is only ever a
    ceiling.
    """
    class _Down:
        async def probe_cluster(self) -> dict:
            raise OSError("metrics briefly unavailable")

    rate = _seed(_Down(), _warm(), lambda _pool: None)

    assert rate.source == "observed_at_concurrency"
