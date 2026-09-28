"""The one-shot was charged for the metrics scrape that priced it.

The agentic loop was fixed for this and the one-shot was not: `run_one_shot` set
`last_progress` at entry and then awaited `seed_decode_rate` inside `dispatch()`, so every
second the scrape spent came out of the silence budget its only turn was measured against.
Found by the 2026-09-28 audit, where `docs/DISPATCH.md` described the fixed ordering for
both paths.

The doubles and the four cases mirror the loop's own regression test, so the two paths are
held to the same claims.
"""

from __future__ import annotations

import asyncio

import pytest
from test_agentic_loop import cfg, entry
from test_the_rate_seed_ran_inside_the_first_turns_stall_clock import (
    SEED_SECONDS,
    STALL,
    SeedingBackend,
    _tick_sleep,
)

from claude_delegate_local import loop


def _run(backend, now, *, stall_timeout=STALL, recorder=None, monkeypatch=None):
    if recorder is not None:
        real = loop.complete_with_retry

        async def recording(*args, **kw):
            left = kw.get("stall_left")
            recorder.append(None if left is None else left())
            return await real(*args, **kw)

        monkeypatch.setattr(loop, "complete_with_retry", recording)

    return asyncio.run(
        loop.run_one_shot(
            cfg(stall_timeout=stall_timeout, dispatch_timeout=3600),
            entry(),
            backend,
            loop.Delegation("do the thing"),
            clock=lambda: now[0],
            tick_sleep=_tick_sleep,
        )
    )


def test_the_one_shot_is_handed_the_whole_stall_budget(monkeypatch):
    """25 seconds of scrape against 30: the unfixed path hands its turn five."""
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=0.0)
    seen: list[float | None] = []

    _run(backend, now, recorder=seen, monkeypatch=monkeypatch)

    assert backend.scrapes == 1, "the seed did not run, so nothing was being measured"
    assert seen, "complete_with_retry was never reached"
    assert seen[0] == float(STALL), (
        f"the one-shot began with {seen[0]}s of a {STALL}s silence budget; the scrape "
        "before it spent the rest"
    )


def test_the_seed_really_does_spend_the_clock(monkeypatch):
    """Negative control: a free scrape would pass the test above in either order."""
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=0.0)
    seen: list[float | None] = []

    _run(backend, now, recorder=seen, monkeypatch=monkeypatch)

    assert now[0] - 1000.0 >= SEED_SECONDS, (
        "the scrape spent nothing, so the ordering under test has no consequence"
    )
    assert seen[0] is not None, "the one-shot passed no stall clock down at all"


def test_an_answer_shorter_than_the_budget_survives_the_scrape_before_it():
    """20 seconds of silence inside 30 is healthy; unfixed, the scrape left five."""
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=20.0)

    result = _run(backend, now)

    assert result.response.text == "done"


def test_an_answer_longer_than_the_budget_still_dies():
    """Moving the clock's start must not disarm it: 35 seconds of silence is a stall."""
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=35.0)

    with pytest.raises(loop.DispatchTimedOut) as caught:
        _run(backend, now)

    assert caught.value.setting == "DELEGATE_STALL_TIMEOUT"
