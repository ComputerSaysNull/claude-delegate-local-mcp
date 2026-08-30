"""The shared admission counters. ADR-0040.

The rule `test_admission.py` sets applies with more force here, because the thing under
test is a *scope*: "the delegation eventually ran" passes identically against a gate that
counts the whole machine and against one that counts nothing at all. So the cross-process
tests below use **two real operating-system processes**, and each one asserts that the
second process is still blocked while the first holds its slot.

`test_a_second_process_is_bounded_by_the_first` carries its own control. Pointed at one
shared file the second acquire must park; pointed at two separate files -- the same code,
the same timings, the only difference being whether the counters are shared -- it must
sail straight through. Without that leg the test would pass just as well against a gate
that blocks for some unrelated reason, which is the failure this repository has already
found four times.

These need real `flock` and a real `/proc`, so they are skipped where those are absent
and are meant to be run inside WSL rather than from the Windows drive.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config
from claude_delegate_local.slots import (
    SharedSlots,
    Totals,
    _identity,
    _is_live,
    _proc_start_time,
    build_slots,
    cross_process_status,
)

try:
    import fcntl
except ImportError:  # pragma: no cover -- Windows
    fcntl = None  # type: ignore[assignment]

posix_only = pytest.mark.skipif(
    fcntl is None or not Path("/proc").is_dir(),
    reason="needs POSIX flock and /proc; run these under WSL, not on the Windows drive",
)


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 5,
        "kv_token_budget": 100_000,
        "large_prefill_tokens": 10_000,
        "max_inflight_large_prefills": 2,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def slots_at(path: Path) -> SharedSlots:
    s = SharedSlots(path)
    s.prepare()
    return s


async def take(g: Admission, tokens: int, *, key: str = "flash", limit: int = 5):
    return await g.acquire(
        tokens, prefill_tokens=tokens, entry_key=key, entry_limit=limit
    )


async def parked(g: Admission, tokens: int, *, key: str = "flash", limit: int = 5):
    """Start an acquire in the background and give a gate that would admit it the chance."""
    task = asyncio.create_task(take(g, tokens, key=key, limit=limit))
    for _ in range(5):
        await asyncio.sleep(0)
    # The cross-process waiter polls rather than being notified, so give it real time to
    # come round again -- otherwise "not done" would only mean "has not looked yet".
    await asyncio.sleep(0.4)
    return task


# ---- the child process ---------------------------------------------------------------
HOLDER = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from claude_delegate_local.admission import Admission
    from claude_delegate_local.config import Config
    from claude_delegate_local.slots import SharedSlots

    async def main() -> None:
        path, tokens, seqs = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
        slots = SharedSlots(path)
        slots.prepare()
        cfg = Config(
            workspace_roots=(".",), max_inflight_seqs=seqs, kv_token_budget=100000,
            large_prefill_tokens=10000, max_inflight_large_prefills=2,
        )
        gate = Admission(cfg, slots)
        await gate.acquire(
            tokens, prefill_tokens=tokens, entry_key="flash", entry_limit=5
        )
        print("held", flush=True)
        sys.stdin.readline()          # hold the slot until the parent says let go
    asyncio.run(main())
    """
)


def start_holder(path: Path, tokens: int, seqs: int) -> subprocess.Popen:
    """A second process holding a real slot in `path`, ready before this returns."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), str(tokens), str(seqs)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready = proc.stdout.readline()
    assert ready.strip() == "held", f"holder never took its slot: {ready!r}"
    return proc


def stop_holder(proc: subprocess.Popen) -> None:
    with contextlib_suppress():
        if proc.stdin is not None:
            proc.stdin.write("go\n")
            proc.stdin.flush()
    proc.wait(timeout=10)


class contextlib_suppress:
    """`contextlib.suppress(Exception)` under a name that says why it is here."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> bool:
        return True


# ---- the headline: one machine, one budget -------------------------------------------
@posix_only
@pytest.mark.asyncio
async def test_a_second_process_is_bounded_by_the_first(tmp_path: Path) -> None:
    """Two processes, one sequence slot between them. The second must wait."""
    shared = tmp_path / "shared.json"
    holder = start_holder(shared, tokens=100, seqs=1)
    try:
        gate = Admission(cfg(max_inflight_seqs=1), slots_at(shared))
        second = await parked(gate, 100)
        assert not second.done(), (
            "a second process was admitted past max_inflight_seqs=1: the gate is "
            "counting this process only, which is the whole bug ADR-0040 exists for"
        )
        second.cancel()
    finally:
        stop_holder(holder)


@posix_only
@pytest.mark.asyncio
async def test_separate_files_do_not_bound_each_other(tmp_path: Path) -> None:
    """The control for the test above: unshared counters must NOT block.

    Same processes, same limits, same timings. If this one also parked, the test above
    would be proving something other than what it claims.
    """
    holder = start_holder(tmp_path / "theirs.json", tokens=100, seqs=1)
    try:
        gate = Admission(cfg(max_inflight_seqs=1), slots_at(tmp_path / "mine.json"))
        second = await parked(gate, 100)
        assert second.done(), (
            "with separate files the second process should not have been blocked; if it "
            "was, the sharing test above is passing for the wrong reason"
        )
        await gate.release(await second)
    finally:
        stop_holder(holder)


@posix_only
@pytest.mark.asyncio
async def test_the_token_budget_is_summed_across_processes(tmp_path: Path) -> None:
    """Rule 2, cross-process: neither request alone exceeds the budget, together they do."""
    shared = tmp_path / "shared.json"
    holder = start_holder(shared, tokens=60_000, seqs=5)
    try:
        gate = Admission(cfg(kv_token_budget=100_000), slots_at(shared))
        second = await parked(gate, 60_000)
        assert not second.done(), "summed tokens across processes passed kv_token_budget"
        second.cancel()
    finally:
        stop_holder(holder)


# ---- reclaim -------------------------------------------------------------------------
@posix_only
@pytest.mark.asyncio
async def test_a_killed_process_leaks_no_slots(tmp_path: Path) -> None:
    """`kill -9` is the ordinary way an editor window goes away. It must cost nothing."""
    shared = tmp_path / "shared.json"
    holder = start_holder(shared, tokens=100, seqs=1)
    gate = Admission(cfg(max_inflight_seqs=1), slots_at(shared))

    blocked = await parked(gate, 100)
    assert not blocked.done(), "precondition: the live holder should be blocking us"

    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)

    lease = await asyncio.wait_for(blocked, timeout=10)
    await gate.release(lease)


@posix_only
@pytest.mark.asyncio
async def test_pid_reuse_cannot_inherit_slots(tmp_path: Path) -> None:
    """A record is keyed by the incarnation of a PID, not by the number alone.

    Forged rather than raced: waiting for the kernel to actually reuse a PID is not a
    test, it is a coin flip. A live PID under a start time that is not its own is exactly
    the state PID reuse produces, so that state is what gets asserted on.
    """
    shared = tmp_path / "shared.json"
    mine = os.getpid()
    real = _proc_start_time(mine)
    assert real is not None, "this platform should have /proc; the skip marker is wrong"

    forged = f"{mine}:{real + 1}"
    shared.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    forged: {
                        "seqs": 99,
                        "tokens": 99_000,
                        "large": 9,
                        "per_entry": {"flash": 99},
                        "updated_at": time.time(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert _is_live(forged) is False, "a mismatched start time must not read as live"
    assert _is_live(_identity()) is True, "this very process must read as live"

    gate = Admission(cfg(max_inflight_seqs=1), slots_at(shared))
    lease = await asyncio.wait_for(take(gate, 100), timeout=5)
    await gate.release(lease)


@posix_only
@pytest.mark.asyncio
async def test_a_corrupt_file_resets_rather_than_wedging(tmp_path: Path) -> None:
    """An unparseable file must not take every delegation on the machine down with it."""
    shared = tmp_path / "shared.json"
    shared.write_text("{not json at all", encoding="utf-8")

    gate = Admission(cfg(), slots_at(shared))
    lease = await asyncio.wait_for(take(gate, 100), timeout=5)
    await gate.release(lease)

    assert json.loads(shared.read_text(encoding="utf-8"))["records"] == {}


# ---- the lock must not stall the loop -------------------------------------------------
@posix_only
@pytest.mark.asyncio
async def test_waiting_for_the_lock_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """A blocking `flock` would freeze delegations that are not waiting for anything.

    The ticker is the assertion. If the lock were taken with a blocking call, the whole
    loop would sit inside it and the ticker would not advance while it was held.
    """
    shared = tmp_path / "shared.json"
    slots_at(shared)
    hold = textwrap.dedent(
        """
        import fcntl, os, sys, time
        fd = os.open(sys.argv[1], os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("locked", flush=True)
        time.sleep(1.5)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", hold, str(shared)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(100):
            await asyncio.sleep(0.01)
            ticks += 1

    gate = Admission(cfg(), slots_at(shared))
    ticking = asyncio.create_task(ticker())
    lease = await asyncio.wait_for(take(gate, 100), timeout=20)
    took = ticks
    ticking.cancel()
    await gate.release(lease)
    proc.wait(timeout=10)

    assert took > 20, (
        f"the event loop only advanced {took} ticks while the lock was held elsewhere; "
        "that is a blocking flock stalling every other delegation in the process"
    )


# ---- what the operator is told --------------------------------------------------------
@posix_only
@pytest.mark.asyncio
async def test_status_reports_the_machine_wide_total(tmp_path: Path) -> None:
    shared = tmp_path / "shared.json"
    holder = start_holder(shared, tokens=7_000, seqs=5)
    try:
        slots = slots_at(shared)
        gate = Admission(cfg(), slots)
        lease = await take(gate, 3_000)

        report = await cross_process_status(slots, "")
        assert report["active"] is True
        assert report["processes_holding_slots"] == 2
        assert report["inflight_tokens"] == 10_000, (
            "the reported total must be the machine's, not this process's share"
        )
        assert gate.status()["inflight_tokens"] == 3_000, (
            "this process's own gauge should still report only its own share"
        )
        await gate.release(lease)
    finally:
        stop_holder(holder)


def test_an_inactive_gate_says_so_rather_than_looking_healthy() -> None:
    """Reported, never assumed. A narrowed scope must be visible in `backend_status`."""
    slots, reason = build_slots(cfg(cross_process_slots=False))
    assert slots is None
    assert "DELEGATE_CROSS_PROCESS_SLOTS" in reason

    report = asyncio.run(cross_process_status(None, reason))
    assert report["active"] is False
    assert report["reason"] == reason


def test_the_shared_path_names_no_endpoint(tmp_path: Path) -> None:
    """The state path is derived from the machine, never from the cluster it protects.

    Keying the file by endpoint would have put a host fragment on the filesystem, which
    is the one place the scanner that guards every other surface does not look.
    """
    slots, reason = build_slots(cfg(slots_dir=str(tmp_path)))
    if slots is None:  # Windows: nothing to inspect, and the reason is the point
        assert reason
        return
    text = str(slots.path)
    assert "http" not in text
    assert ":" not in text.replace(str(tmp_path), ""), "a host:port shape reached the path"


# ---- local-only behaviour is unchanged --------------------------------------------------
@pytest.mark.asyncio
async def test_without_a_shared_file_the_old_behaviour_is_exact() -> None:
    """No file means the pre-ADR-0040 gate, reproduced rather than approximated."""
    gate = Admission(cfg(max_inflight_seqs=1))
    first = await take(gate, 100)
    second = asyncio.create_task(take(gate, 100))
    for _ in range(5):
        await asyncio.sleep(0)

    assert not second.done()
    await gate.release(first)
    await gate.release(await asyncio.wait_for(second, timeout=1))


def test_totals_default_to_empty() -> None:
    t = Totals()
    assert (t.seqs, t.tokens, t.large, t.per_entry) == (0, 0, 0, {})
