"""A staggered burst outlasted the fixed hold, so its members labelled themselves too low.

`admission_idle_hold` shipped as a flat wait: the first member of a fan-out sleeps ten
seconds, re-reads the gate, and records whatever it finds. That is right only if the burst
lands inside one window.

It does not. Measured 2026-09-17 over two six-wide bursts from one client message, the gaps
between consecutive arrivals were `[5.2, 5.9, 6.4, 5.5, 5.4]` spanning 28.4s, and
`[4.8, 5.5, 8.0, 7.0, 7.2]` spanning 32.4s. A ten-second window closes with two or three of
the six counted, so the sample is filed under a contention it never met -- `rate-history.json`
held `[3, 18.869...]`, a six-way rate under a label of 3.

The fix is a debounce rather than a longer fixed wait. Two rules:

  * release as soon as the gate **fills**, because a full gate has already told you the
    burst's size and waiting out the rest of a timer buys nothing; and
  * otherwise release after one whole window with **no new arrival**.

Why not simply raise the fixed hold to 30s: the hold fires only when the gate is found idle,
which is the single interactive delegation, so a fixed 30s makes that call pay 30s for a
burst that never comes. At one window of quiet the solo call sits exactly where today's fixed
hold puts it -- no regression -- while a burst is counted in full.

The window count is the ceiling, so no separate cap is needed: the fill rule bounds the worst
case, and the only way to run longer is arrivals that keep coming while earlier ones complete,
where the cost is one call's dispatch latency rather than a stuck gate.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

from claude_delegate_local import admission as adm
from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config

# Captured before any test patches the attribute, so a harness can yield to the loop
# without the yield being recorded as one of the hold's own windows.
_real_sleep = asyncio.sleep


def held_gate(monkeypatch, **over):
    """A gate whose hold is observable: each window is recorded rather than waited out."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(adm.asyncio, "sleep", fake_sleep)
    kw = {"workspace_roots": (".",), "max_inflight_seqs": 5, "kv_token_budget": 100_000}
    kw.update(over)
    return Admission(Config(**kw)), slept  # type: ignore[arg-type]


async def take(g, tokens=1000, *, key="flash", limit=5):
    return await g.acquire(tokens, entry_key=key, entry_limit=limit)


async def arrive(g, count: int, started: list) -> None:
    """Admit `count` siblings *concurrently*, and return once each holds its slot.

    Concurrently, and not by awaiting each `acquire` inline, because inline is a shape
    the gate cannot produce: a sibling would run to completion inside the window that is
    supposed to be counting it. That difference is invisible while only one member ever
    waits, and decides the answer as soon as more than one does -- a member that waits for
    the burst cannot do so inside the wait it is joining.

    Returns only once the slots are taken, so the window that follows reads a settled
    number rather than racing the tasks it just started.
    """
    want = g.status()["inflight_seqs"] + count
    started.extend(asyncio.create_task(take(g)) for _ in range(count))
    while g.status()["inflight_seqs"] < want:
        await _real_sleep(0)


async def test_the_hold_extends_while_a_burst_is_still_arriving(monkeypatch):
    """The fix, and the shape the fixed hold cannot produce.

    One sibling lands during each of the first two windows, so the hold has to wait a third
    to discover the quiet. Against the flat sleep this is exactly one window and the burst
    is recorded at two of five.
    """
    g, slept = held_gate(monkeypatch, admission_idle_hold=10.0)
    arrivals = [1, 1]
    started: list = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if arrivals:
            arrivals.pop()
            await arrive(g, 1, started)

    monkeypatch.setattr(adm.asyncio, "sleep", fake_sleep)

    lease = await take(g)

    assert arrivals == [], "no sibling arrived, so this proves nothing"
    assert len(slept) == 3, (
        "the hold must wait a further window after each arrival, and one more to "
        f"establish the quiet -- waited {len(slept)}"
    )
    assert lease.seqs_at_grant >= 2, "every sibling that arrived must be counted"
    await asyncio.gather(*started)


async def test_the_hold_releases_as_soon_as_the_gate_fills(monkeypatch):
    """The ceiling. A full gate has told you the burst's size; waiting longer buys nothing.

    Two siblings land in each of the first two windows against a cap of five, so the gate
    is full at the end of the second and the hold must stop there.

    The count discriminates in both directions, which asserting a single window would not:
    the flat hold waits one and records two of five, while a debounce carrying no fill rule
    waits a third for a quiet window the burst has already earned.
    """
    g, slept = held_gate(monkeypatch, admission_idle_hold=10.0)
    waves = [2, 2]
    started: list = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if waves:
            await arrive(g, waves.pop(0), started)

    monkeypatch.setattr(adm.asyncio, "sleep", fake_sleep)

    lease = await take(g)

    assert waves == [], "the siblings never arrived, so this proves nothing"
    assert len(slept) == 2, (
        f"a full gate must release without a quiet window -- waited {len(slept)}"
    )
    assert lease.seqs_at_grant == 4, "a full gate must record every sibling"
    await asyncio.gather(*started)


async def test_a_quiet_idle_gate_still_pays_exactly_one_window(monkeypatch):
    """Control, and the promise that this is not a regression for the solo call.

    Nothing arrives, so the debounce settles in one window -- exactly where the flat hold
    already put the single interactive delegation.
    """
    g, slept = held_gate(monkeypatch, admission_idle_hold=10.0)

    await take(g)

    assert slept == [10.0]


async def test_a_busy_gate_still_does_not_hold(monkeypatch):
    """Control. Concurrency is already known, so the debounce must not introduce latency."""
    g, slept = held_gate(monkeypatch, admission_idle_hold=10.0)
    await take(g)
    slept.clear()

    await take(g)

    assert slept == [], "a busy gate must not pay the hold"


async def test_the_hold_is_still_skipped_entirely_when_disabled(monkeypatch):
    """Control. 0 has to mean no wait at all, not one zero-length window."""
    g, slept = held_gate(monkeypatch, admission_idle_hold=0.0)

    await take(g)

    assert slept == []
