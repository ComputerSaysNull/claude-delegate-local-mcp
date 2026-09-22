"""A burst spread across processes priced every arm at the position it arrived in.

Within one process this was already fixed: a member admitted while a burst wait is open
takes that wait's answer, so three simultaneous arrivals all price at three. The wait is
an `asyncio.Future` on the gate, which is exactly as far as it reaches -- one process.

Across processes it was not, and that is the ordinary shape rather than an exotic one,
because `run` fans out as separate processes. Measured, three arms from three processes
priced 2, 3 and 4 where all three ran at three: each later arm sees only the siblings
ahead of it, and `seqs_at_grant + waiting_at_grant + 1` is therefore its own arrival
position rather than the burst's size. The rate memory is keyed by that number, so a
six-way rate gets filed under a contention it never met.

The cause is that the shared file said nothing about a wait being open. It carried the
counters and the queue, so an arm could see its siblings but not that one of them was
already counting them all.

The fix puts the flag inside a *record*, beside that process's counters. Records are keyed
`(pid, start_time)` and reaped the moment the process stops, so a process that dies holding
an open wait takes its flag with it -- no second staleness rule, and nothing to expire. An
arm that finds a wait open elsewhere counts the burst itself instead of returning its own
snapshot: there is no future to await across a process boundary, and both windows run at
the same time over the same shared totals, so they settle on the same number.

**Every test here fails on an assertion about a price, never on a missing symbol.** A
scenario is set up by writing the shared document by hand -- the flag as the literal key
`burst_wait`, never through the new writer and never through the constant the reader names
it by. Two things follow, both deliberate. Unfixed code reads those documents perfectly
well; it ignores an unknown key in a record and mis-prices, which is the defect asserted.
And the tests are written against the file format two processes agree on rather than
against this implementation, so renaming the constant without changing the format on disk
is a failure here -- which is the point, because the format is the contract.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from claude_delegate_local import slots as slots_module
from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config
from claude_delegate_local.slots import SharedSlots, _identity

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover -- Windows
    fcntl = None  # type: ignore[assignment]

# The same guard `tests/test_slots.py` uses, and for the same two reasons: there is no
# `flock` off POSIX, and a record is only kept while `/proc` says its PID is alive. On
# Windows layer 1 also refuses everything under `tmp_path`. Run these under WSL.
posix_only = pytest.mark.skipif(
    fcntl is None or not Path("/proc").is_dir(),
    reason="needs POSIX flock and /proc; run these under WSL, not on the Windows drive",
)

# The flag as it appears on disk. Written out rather than imported: see the module
# docstring -- these tests are a check on the file format, not on our name for it.
BURST_ON_DISK = "burst_wait"

# Long enough that arms started from one `gather` all land inside the first window, short
# enough that the suite does not feel it.
HOLD = 0.05

# A hold nothing should ever wait out. A test that joins a wait it must not join blocks
# for this, and the `wait_for` around it turns that into a failure rather than a delay.
NEVER = 5.0


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 5,
        "kv_token_budget": 100_000,
        # Zero by default: a fixture that drives the gate directly is testing a rule, not
        # the hold, and a hold left on would time every acquire in it. Each test below
        # that is about the hold asks for one explicitly.
        "admission_idle_hold": 0.0,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


@contextlib.contextmanager
def live_pids(count: int) -> Iterator[list[int]]:
    """`count` PIDs that `/proc` agrees are alive, for records to be filed under."""
    procs = [subprocess.Popen(["sleep", "60"]) for _ in range(count)]
    try:
        yield [p.pid for p in procs]
    finally:
        for p in procs:
            p.kill()
            p.wait()


def slots_as(path: Path, pid: int | None = None) -> SharedSlots:
    """The shared file, seen as the process owning `pid` sees it."""
    shared = SharedSlots(path)
    shared.prepare()
    if pid is not None:
        shared._me = _identity(pid)
    return shared


def arm(path: Path, pid: int | None = None, **over) -> Admission:
    """A gate that files its slots under another process's identity.

    Three real server processes would be three interpreter launches and a lot of timing.
    What makes a process distinct *to this code* is only two things: the key its record is
    filed under, and the fact that its `_holding` future is its own. Separate `Admission`
    objects over one shared file, each claiming a different live PID, have both -- and the
    PIDs are real and alive, so the records are kept exactly as a real server's are.
    """
    return Admission(cfg(**over), slots_as(path, pid))


async def take(g: Admission, tokens: int = 100):
    return await g.acquire(tokens, entry_key="flash", entry_limit=5)


def priced_at(lease) -> int:
    """The number `server.run_delegation` derives from a lease, and the rate memory's key."""
    return lease.seqs_at_grant + lease.waiting_at_grant + 1


def held(seqs: int, *, counting: bool = False) -> dict[str, Any]:
    """One process's record as it appears on disk, optionally counting a burst."""
    record: dict[str, Any] = {
        "seqs": seqs,
        "tokens": 1_000 * seqs,
        "per_entry": {"flash": seqs},
        "updated_at": time.time(),
    }
    if counting:
        record[BURST_ON_DISK] = True
    return record


def publish(path: Path, records: dict[str, dict[str, Any]], *, version: int) -> None:
    """Write the whole document, the way some other process would have left it."""
    path.write_text(
        json.dumps({"version": version, "records": records, "next_ticket": 0}),
        encoding="utf-8",
    )


async def priced_by(gate: Admission, *, timeout: float) -> tuple[int, int]:
    """What an arrival meeting a gate with one slot already on it settles on.

    `(1, 0)` going in is a request that was admitted against one sibling and nobody
    queued -- the pre-grant snapshot an arm is handed. What comes back is what it will
    report, so the whole of the defect is visible in the difference between the two.
    """
    return await asyncio.wait_for(
        gate._settle_burst(1, 0, tokens=100, entry_key="flash", elapsed=0.0),
        timeout=timeout,
    )


# ---- the headline --------------------------------------------------------------------
@posix_only
async def test_every_arm_of_a_burst_across_processes_prices_on_the_whole_burst(
    tmp_path: Path,
) -> None:
    """Three processes arriving together must all price at three.

    Unfixed, only the arm that found the gate idle counts the burst; the other two return
    the snapshot they were admitted against, which is how many siblings happened to be
    ahead of them.
    """
    shared = tmp_path / "shared.json"
    with live_pids(3) as pids:
        arms = [arm(shared, pid, admission_idle_hold=HOLD) for pid in pids]

        leases = await asyncio.gather(*(take(g) for g in arms))

        try:
            assert [priced_at(x) for x in leases] == [3, 3, 3], (
                "an arm priced on the siblings ahead of it rather than on the burst: "
                f"{[priced_at(x) for x in leases]}"
            )
        finally:
            for gate, lease in zip(arms, leases, strict=True):
                await gate.release(lease)


# ---- the flag belongs to a process, and goes when it does ------------------------------
@posix_only
async def test_a_wait_nobody_is_holding_is_not_joined(tmp_path: Path) -> None:
    """The reaping *is* the expiry, and the two legs here are each other's control.

    With the flag-holder alive, an arrival joins and prices on the three slots it found,
    not on the one it was admitted against -- so the second leg is asking a question the
    code can answer. With its record gone, the same arrival must fall straight through: a
    wait whose process has stopped is nobody's wait, and joining it would mean waiting out
    a window for an answer no one will ever publish.

    Both legs are prices, and the first is where unfixed code fails: it reads the same
    document, ignores the flag in it, and hands back the snapshot it was given.
    """
    shared = tmp_path / "shared.json"
    holder_proc = subprocess.Popen(["sleep", "60"])
    try:
        # Filled as another process would have left it, before anything here reads it.
        publish(
            shared,
            {_identity(holder_proc.pid): held(3, counting=True)},
            version=slots_module._SCHEMA_VERSION,
        )

        joined = await priced_by(arm(shared, admission_idle_hold=HOLD), timeout=NEVER)

        assert joined == (2, 0), (
            "an arrival meeting an open wait in another process must count the burst it "
            f"joins -- three slots held, so two besides itself -- and got {joined}; "
            "(1, 0) is the pre-grant snapshot handed in, i.e. its arrival position"
        )

        holder_proc.kill()
        # Reaped, not merely signalled. A zombie still has a `/proc/<pid>/stat` carrying
        # the same start time, so the record would read as live and the flag with it.
        holder_proc.wait()

        alone = await priced_by(arm(shared, admission_idle_hold=NEVER), timeout=0.5)

        assert alone == (1, 0), (
            "a dead process's wait was inherited; its record is gone, so there is "
            "nothing left to join and nothing to wait for"
        )
    finally:
        with contextlib.suppress(Exception):
            holder_proc.kill()
            holder_proc.wait()


# ---- both directions of the file's version ---------------------------------------------
@posix_only
async def test_the_file_version_does_not_move_for_the_flag_and_is_not_a_gate(
    tmp_path: Path,
) -> None:
    """An additive field inside a record, so neither direction needs a version.

    Pinned because the tempting alternative -- bump the version, refuse what does not
    match -- is the outage this file's docstring rules out: a process on the machine is
    still holding the slots the refusing reader would zero. So the number stays where it
    is, and the reader must go on ignoring it.

    Three claims. An unrecognised version must still be read rather than reset. A flag
    inside a record of such a document must still be acted on, which is asserted as the
    price an arrival gets rather than as anything about a symbol. And a document with no
    flag anywhere -- every document an older writer produces -- must leave an arrival
    pricing exactly as it did before this field existed, and waiting for nothing.
    """
    assert slots_module._SCHEMA_VERSION == 1, (
        "the flag is additive and both directions already read it sanely, so the version "
        "must not move; see the reason beside the flag's constant in slots.py"
    )
    from_the_future = slots_module._SCHEMA_VERSION + 1

    shared = tmp_path / "shared.json"
    with live_pids(2) as (flagged_pid, plain_pid):
        records = {
            _identity(flagged_pid): held(2, counting=True),
            _identity(plain_pid): held(1),
        }
        publish(shared, records, version=from_the_future)

        totals, processes, _ = await slots_as(shared).snapshot()

        assert (processes, totals.seqs, totals.tokens) == (2, 3, 3_000), (
            "a version this reader does not recognise reset a file whose slots two live "
            "processes are still holding"
        )

        joined = await priced_by(arm(shared, admission_idle_hold=HOLD), timeout=NEVER)

        assert joined == (2, 0), (
            "a flag inside a record of a document whose version this reader does not "
            f"recognise must still be acted on; got {joined}, which is the arrival "
            "position the pre-flag behaviour reports"
        )

        # The same document with every flag removed: what an older writer produces.
        publish(
            shared,
            {key: held(record["seqs"]) for key, record in records.items()},
            version=slots_module._SCHEMA_VERSION,
        )

        alone = await priced_by(arm(shared, admission_idle_hold=NEVER), timeout=0.5)

        assert alone == (1, 0), (
            "a document written before this field made an arrival wait for, and price "
            "on, a burst nobody opened"
        )

# ---- what the flag must not break -----------------------------------------------------
@posix_only
async def test_a_cancellation_while_announcing_does_not_wedge_the_gate(tmp_path: Path) -> None:
    """Publishing the flag is an await, and it sits between two things that must not part.

    `_holding` is created, then the wait is announced, then the window runs inside a
    `try` that settles the future on every exit. Put the announce *outside* that block and
    a cancellation delivered during it leaves the future created and never settled -- so
    every later burst in this process joins a wait nobody will finish, and the slot that
    was already taken leaks with it, the release being skipped too.

    Asserted as a second burst returning at all. A hang is the failure, so the assertion
    is a timeout rather than a value: waiting for ever is exactly the defect.
    """
    path = tmp_path / "slots.json"
    gate = arm(path, admission_idle_hold=0.2)

    async def cancelled(*_a: object, **_k: object) -> None:
        raise asyncio.CancelledError

    original = gate._slots.open_burst_wait
    gate._slots.open_burst_wait = cancelled  # type: ignore[method-assign]

    # An IDLE gate, because that is the only path that announces: with a sibling already
    # on the gate `_settle_burst` returns the snapshot before it ever publishes anything.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            gate._settle_burst(0, 0, tokens=100, entry_key="flash", elapsed=0.0),
            timeout=5.0,
        )

    # The future the first burst created must not outlive it unsettled.
    assert gate._holding is None or gate._holding.done(), (
        "the burst wait was left unsettled, so the next arrival will await it for ever"
    )

    # The observable half: a second burst must still answer. Unfixed, this awaits the
    # dangling future and never returns, so it raises TimeoutError rather than asserting.
    gate._slots.open_burst_wait = original  # type: ignore[method-assign]
    assert await asyncio.wait_for(
        gate._settle_burst(0, 0, tokens=100, entry_key="flash", elapsed=0.0),
        timeout=5.0,
    ) == (0, 0)
