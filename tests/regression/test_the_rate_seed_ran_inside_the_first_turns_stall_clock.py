"""Turn 1 was charged for the metrics scrape that priced it.

`run_agentic_loop` starts the no-progress clock with `last_progress = clock()` and then
seeds the decode rate from `/metrics` before entering the turn loop. The seed was awaited
*after* the clock started, so every second the scrape spent came out of turn 1's silence
budget -- and the comment above the seed claimed the opposite, that placing it there
"keeps the network call outside the stall clock the turn is about to be measured
against". It did not.

It matters because the scrape is the one network call on this path that talks to an
endpoint which may itself be wedged, and `seed_decode_rate` swallows every failure by
design. A blackholed `/metrics` therefore cost the first turn its whole budget and was
reported as a stall in the model, which is the wrong layer to send an operator to.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest
from test_agentic_loop import cfg, entry, says

from claude_delegate_local import loop

SEED_SECONDS = 25.0
STALL = 30


class SeedingBackend:
    """Answers one turn, and charges the clock for the metrics scrape that precedes it.

    `probe_cluster` is the only thing `seed_decode_rate` calls, so it is the only place a
    slow endpoint can burn the clock before the first turn -- and the doubles elsewhere
    do not define it at all, which makes the scrape free and the bug invisible.
    """

    def __init__(self, now: list[float], *, seed_seconds: float,
                 silent_seconds: float) -> None:
        self.now = now
        self.seed_seconds = seed_seconds
        self.silent_seconds = silent_seconds
        self.scrapes = 0

    async def probe_cluster(self):
        self.scrapes += 1
        self.now[0] += self.seed_seconds
        return {"decode_tokens_per_second_since_boot": 20.0, "requests_running": 1}

    async def complete(self, request, *, on_token=None):
        # Silence, a second at a time, yielding so the deadline watchdog beside this call
        # gets to read the clock. Without the yield the loop never suspends, the watchdog
        # never runs, and the stall deadline cannot fire whatever the budget says.
        spent = 0.0
        while spent < self.silent_seconds:
            self.now[0] += 1.0
            spent += 1.0
            await asyncio.sleep(0)
        return says("done")

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


async def _tick_sleep(_seconds: float) -> None:
    """The watchdog's wait, yielding without moving the clock.

    The backend owns the clock here, because what is under test is how much budget turn 1
    starts with rather than how fast it is spent -- and two writers to one fake clock make
    the answer depend on interleaving. The seam is still needed: `_until_deadline` waits on
    the wall without it, so the watchdog would never poll and a turn with no budget left
    would finish happily.
    """
    await asyncio.sleep(0)


def _run(backend, now, *, stall_timeout=STALL, recorder=None, monkeypatch=None):
    if recorder is not None:
        real = loop.complete_with_retry

        async def recording(*args, **kw):
            # Read before the call, which is the moment the question is about: how much
            # silence budget did turn 1 actually get handed.
            left = kw.get("stall_left")
            recorder.append(None if left is None else left())
            return await real(*args, **kw)

        monkeypatch.setattr(loop, "complete_with_retry", recording)

    return asyncio.run(
        loop.run_agentic_loop(
            cfg(stall_timeout=stall_timeout, dispatch_timeout=3600),
            entry(),
            backend,
            loop.Delegation("do the thing"),
            allowed=frozenset(),
            max_turns=2,
            clock=lambda: now[0],
            tick_sleep=_tick_sleep,
        )
    )


def test_the_first_turn_is_handed_the_whole_stall_budget(monkeypatch):
    """The bug, measured at the seam it enters through.

    25 seconds of scrape against a 30 second budget, so the unfixed loop hands turn 1
    five seconds and this reads 5.0 instead of 30.0.
    """
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=0.0)
    seen: list[float | None] = []

    _run(backend, now, recorder=seen, monkeypatch=monkeypatch)

    assert backend.scrapes == 1, "the seed did not run, so nothing was being measured"
    assert seen, "complete_with_retry was never reached"
    assert seen[0] == float(STALL), (
        f"turn 1 began with {seen[0]}s of a {STALL}s silence budget; the scrape before it "
        "spent the rest"
    )


def test_the_seed_really_does_spend_the_clock(monkeypatch):
    """The negative control, and the reason the test above can fail at all.

    A `probe_cluster` that cost nothing would satisfy the assertion forever, whichever
    order the two statements are in. This pins that the double charges the clock and that
    the loop awaits it before the first turn.
    """
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=0.0)
    seen: list[float | None] = []

    _run(backend, now, recorder=seen, monkeypatch=monkeypatch)

    assert now[0] - 1000.0 >= SEED_SECONDS, (
        "the scrape spent nothing, so the ordering under test has no consequence"
    )
    assert seen[0] is not None, "the loop passed no stall clock down at all"


def test_a_turn_shorter_than_the_budget_survives_the_scrape_before_it():
    """The consequence, from the outside: no monkeypatch, just an answer.

    20 seconds of silence inside a 30 second budget is healthy. Against the unfixed loop
    the 25 second scrape has already taken all but five of them, so the turn is cancelled
    and this raises instead of answering.
    """
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=20.0)

    result = _run(backend, now)

    assert result.response.text == "done"


def test_a_turn_longer_than_the_budget_still_dies():
    """The other control. Moving the clock's start must not disarm it.

    35 seconds of silence against a 30 second budget is a stall whenever the clock
    starts, and a fix that simply stopped counting would pass every test above.
    """
    now = [1000.0]
    backend = SeedingBackend(now, seed_seconds=SEED_SECONDS, silent_seconds=35.0)

    with pytest.raises(loop.DispatchTimedOut) as caught:
        _run(backend, now)

    assert caught.value.setting == "DELEGATE_STALL_TIMEOUT"
