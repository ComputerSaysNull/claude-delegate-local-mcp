"""A burst flag stranded by a failed close held every arrival elsewhere (PLAN M14.5).

`_announce_burst` is best effort, so a close that fails against an unreachable file leaves
the flag set. It was a bare `True` with no expiry, and a record is kept for as long as its
process holds slots, so every other process read an open wait nobody was counting -- and
each arrival at a busy gate joined it and waited out one `admission_idle_hold` for nothing,
until the owning process went idle or exited.

The flag now carries the time it expires, two windows ahead, and the counter renews it each
window it goes on counting. As in the sibling file, scenarios are written to disk by hand:
the file format is the contract two processes agree on.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from test_a_burst_spanning_processes_priced_on_arrival_order import (
    BURST_ON_DISK,
    HOLD,
    NEVER,
    arm,
    held,
    live_pids,
    posix_only,
    priced_by,
    publish,
)

from claude_delegate_local import slots as slots_module
from claude_delegate_local.slots import _identity


def _flagged(shared: Path, pid: int, flag: object) -> None:
    record = held(3)
    record[BURST_ON_DISK] = flag
    publish(shared, {_identity(pid): record}, version=slots_module._SCHEMA_VERSION)


@posix_only
async def test_an_expired_flag_is_not_joined(tmp_path: Path) -> None:
    """The defect. Unfixed, any truthy flag is an open wait, so this arrival joins it and
    sleeps out a window nobody will close -- the `wait_for` turns that into a failure."""
    shared = tmp_path / "shared.json"
    with live_pids(1) as (pid,):
        _flagged(shared, pid, time.time() - 1.0)
        alone = await priced_by(arm(shared, admission_idle_hold=NEVER), timeout=0.5)
    assert alone == (1, 0), f"an expired flag was joined, pricing at {alone}"


@posix_only
async def test_a_flag_inside_its_window_is_still_joined(tmp_path: Path) -> None:
    """The control: the expiry must not stop an arrival joining a burst still counting."""
    shared = tmp_path / "shared.json"
    with live_pids(1) as (pid,):
        _flagged(shared, pid, time.time() + 60.0)
        joined = await priced_by(arm(shared, admission_idle_hold=HOLD), timeout=NEVER)
    assert joined == (2, 0), f"a live flag was not joined, pricing at {joined}"


@posix_only
async def test_an_older_servers_bare_flag_is_still_joined(tmp_path: Path) -> None:
    """A server not yet upgraded writes `True`. Reading it as open is what it reads too."""
    shared = tmp_path / "shared.json"
    with live_pids(1) as (pid,):
        _flagged(shared, pid, True)
        joined = await priced_by(arm(shared, admission_idle_hold=HOLD), timeout=NEVER)
    assert joined == (2, 0)


@posix_only
async def test_the_flag_written_is_an_expiry_two_windows_ahead(tmp_path: Path) -> None:
    shared = tmp_path / "shared.json"
    gate = arm(shared, admission_idle_hold=HOLD)
    before = time.time()
    await gate._announce_burst(open_wait=True)
    records = json.loads(shared.read_text(encoding="utf-8"))["records"]
    (flag,) = [r[BURST_ON_DISK] for r in records.values() if BURST_ON_DISK in r]
    assert flag is not True and isinstance(flag, float), f"no expiry on disk: {flag!r}"
    assert before < flag <= time.time() + 2 * HOLD


@posix_only
async def test_a_burst_still_counting_renews_its_flag(tmp_path: Path) -> None:
    """Without renewal a burst longer than two windows would expire under its own count,
    and a sibling in another process would stop joining it."""
    gate = arm(tmp_path / "shared.json", admission_idle_hold=HOLD)
    views = iter([(1, 1, 0), (2, 2, 0), (2, 2, 0), (3, 3, 0), (3, 3, 0), (3, 3, 0)])

    async def arriving():
        return next(views)

    opened = 0
    original = gate._slots.open_burst_wait

    async def counting(*a, **k):
        nonlocal opened
        opened += 1
        await original(*a, **k)

    gate._burst_view = arriving  # type: ignore[method-assign]
    gate._slots.open_burst_wait = counting  # type: ignore[method-assign]
    await asyncio.wait_for(
        gate._settle_burst(0, 0, tokens=100, entry_key="flash", elapsed=0.0), timeout=NEVER)
    assert opened == 3, f"three windows, two of them continuing, announced {opened} time(s)"
