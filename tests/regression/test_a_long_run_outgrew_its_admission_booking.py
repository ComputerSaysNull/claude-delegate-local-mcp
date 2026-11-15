"""Admission booked a delegation once, at grant, and never grew it with the run.

The gate prices a delegation as its KV footprint -- the opening prompt estimate plus the
reply budget -- and fixes that figure in the lease. A long agentic run's prompt grows by
roughly 36x over the loop (3.8k to 317k measured), so by its later turns the booking says
a fraction of the memory the delegation actually holds, and the gate admits newcomers
against a pool that is already spent. `Admission.grow(lease, tokens)` is the API that lets
a run raise its booking as it grows; the tests below are the red for it.

Each test asserts a behaviour of that API, and each carries the check that would fail if
the API were a no-op: the refusal a growth causes, the file figure the release must give
back, the peak that must rise. A `grow` that returned its argument and did nothing would
fail every one of them.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from claude_delegate_local.admission import Admission, AdmissionTimedOut
from claude_delegate_local.config import Config
from claude_delegate_local.slots import SharedSlots, _identity, cross_process_status

pytestmark = pytest.mark.asyncio


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        # Well above the one lease these tests hold, so a refusal is the token rule and
        # never the sequence cap: a test that passed on rule one would prove nothing
        # about a grown booking.
        "max_inflight_seqs": 5,
        "admission_idle_hold": 0.0,
        "admission_starvation_grace": 0.0,
        "kv_token_budget": 1_000_000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def slots_at(path: Path) -> SharedSlots:
    s = SharedSlots(path)
    s.prepare()
    return s


async def take(g: Admission, tokens: int, *, key: str = "flash", limit: int = 5):
    return await g.acquire(
        tokens,
        entry_key=key,
        entry_limit=limit,
    )


async def settle(n: int = 8) -> None:
    """Let a parked acquire reach the condition and test the predicate."""
    for _ in range(n):
        await asyncio.sleep(0)


def booked_in_file(path: Path) -> int:
    """What the shared file says this process is holding, 0 when it has no record."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    record = (doc.get("records") or {}).get(_identity()) or {}
    return int(record.get("tokens", 0))


def record_present(path: Path) -> bool:
    """Whether this process still has a record in the shared file at all."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    return _identity() in (doc.get("records") or {})


async def test_growth_makes_a_newcomer_that_would_have_fitted_wait(tmp_path: Path):
    """The bug: a 36x-grown run still books its opening size, so the gate admits past the pool.

    Lease A is admitted at 100,000 against a 1,000,000 budget and grows to 800,000. A
    300,000 request fits the budget against A's original booking and must not fit against
    the grown one, so it has to be refused by the token rule rather than admitted.
    """
    path = tmp_path / "shared.json"
    g = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    held = await take(g, 100_000)

    await g.grow(held, 800_000)

    with pytest.raises(AdmissionTimedOut) as caught:
        await g.acquire(300_000, entry_key="flash", entry_limit=5, deadline=0.0)
    assert caught.value.rule == "kv_token_budget", (
        f"the newcomer was refused by {caught.value.rule}, not by the grown booking, so "
        "this test is not about growth"
    )

    await g.release(held)


async def test_growth_makes_a_parked_newcomer_stay_parked(tmp_path: Path):
    """The same refusal seen as a wait: a task that must still be running after growth.

    A gate that admitted everything would raise nothing above; this one watches the
    acquire never complete, which is the shape `test_admission.py` uses for every rule.
    """
    path = tmp_path / "shared.json"
    g = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    held = await take(g, 100_000)

    await g.grow(held, 800_000)

    waiter = asyncio.create_task(take(g, 300_000))
    await settle()
    assert not waiter.done(), (
        "a 300,000-token acquire was granted against a lease grown to 800,000 of a "
        "1,000,000 budget: growth did not reach the gate's token rule"
    )
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await g.release(held)


async def test_releasing_a_grown_lease_gives_back_the_grown_booking(tmp_path: Path):
    """The negative control: a release that subtracts the original amount leaks 700,000.

    `release` subtracts `lease.tokens`, so a `grow` that raises the booking locally but
    leaves the lease -- or the file -- at 100,000 leaves this process and the machine
    counting a slot nobody holds, and every later delegation waits for it.
    """
    path = tmp_path / "shared.json"
    slots = slots_at(path)
    g = Admission(cfg(kv_token_budget=1_000_000), slots)
    held = await take(g, 100_000)

    await g.grow(held, 800_000)
    # Precondition, or the release assertion below would pass for the wrong reason: a
    # grow that never wrote the file would also end at zero after a grown subtraction.
    assert booked_in_file(path) == 800_000, (
        f"after growing to 800,000 the shared file still books {booked_in_file(path)} tokens"
    )

    await g.release(held)

    assert g.status()["inflight_tokens"] == 0, (
        f"this process still counts {g.status()['inflight_tokens']} in-flight tokens "
        "after releasing the lease it grew"
    )
    assert booked_in_file(path) == 0, (
        f"the shared file still books {booked_in_file(path)} tokens for this process "
        "after releasing the lease it grew"
    )
    totals, processes, _ = await slots.snapshot()
    assert totals.tokens == 0
    assert processes == 0, "a released record was left behind in the shared file"


async def test_growth_raises_the_peak_inflight_tokens_it_reached(tmp_path: Path):
    """`peak_inflight_tokens` is how an operator learns the booking is too small.

    A peak that only ever reports the figure at grant says the ceiling is fine while a
    run holds several times it. It must rise with the growth, and stay risen afterwards.
    """
    path = tmp_path / "shared.json"
    g = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    held = await take(g, 100_000)
    assert g.status()["peak_inflight_tokens"] == 100_000, "precondition: the peak at grant"

    returned = await g.grow(held, 800_000)

    assert returned == 800_000, f"grow returned {returned}, not the lease's booking after the call"
    assert g.status()["peak_inflight_tokens"] == 800_000, (
        f"the peak stayed at {g.status()['peak_inflight_tokens']} while the lease grew "
        "to 800,000: the high-water mark is not measuring what a run actually held"
    )

    await g.release(held)
    assert g.status()["peak_inflight_tokens"] == 800_000, (
        "the peak fell back on release; a high-water mark must not be the live counter"
    )


async def test_a_smaller_growth_request_leaves_the_booking_where_it_was(tmp_path: Path):
    """Growth is a ceiling raise, not an assignment: a run's prompt can shrink a little.

    Asking for less than is already booked must not lower it, since the memory the run
    still holds is the larger figure.
    """
    path = tmp_path / "shared.json"
    g = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    held = await take(g, 100_000)
    await g.grow(held, 800_000)

    returned = await g.grow(held, 50_000)

    assert returned == 800_000, (
        f"a smaller request of 50,000 returned {returned} rather than the booking of "
        "800,000 it did not change"
    )
    assert g.status()["inflight_tokens"] == 800_000, (
        f"the booking dropped to {g.status()['inflight_tokens']} on a smaller request"
    )
    assert g.status()["peak_inflight_tokens"] == 800_000

    await g.release(held)


async def test_an_unshared_lease_grows_without_touching_the_slots_file(tmp_path: Path):
    """A lease marked unshared has no slot in the file, so growth must not write one.

    `shared=False` is what `acquire` sets when the file was unreachable at grant: the
    slot exists only in this process's counters. Growing it by the delta into the file
    would add tokens this process never published, inflating every other process's view.
    """
    path = tmp_path / "shared.json"
    g = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    held = await take(g, 100_000)
    unshared = replace(held, shared=False)
    assert booked_in_file(path) == 100_000, "precondition: the grant reached the file"

    returned = await g.grow(unshared, 800_000)

    assert returned == 800_000
    assert g.status()["inflight_tokens"] == 800_000, (
        "the local booking did not grow, so the file assertion below proves nothing"
    )
    assert booked_in_file(path) == 100_000, (
        f"an unshared lease's growth wrote {booked_in_file(path)} tokens into the shared "
        "file, which had booked 100,000 for it"
    )

    await g.release(held)


async def test_growth_after_release_returns_zero_and_revives_no_record(tmp_path: Path):
    """A lease already given back is not held, so growth must be a silent no-op.

    The trap is `SharedSlots._mine`, which recreates a record. A `grow` that reaches for
    it after the release reaped the record leaves a phantom holding tokens for a slot
    nobody holds, and the machine waits on it until this process exits.
    """
    path = tmp_path / "shared.json"
    g = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    held = await take(g, 100_000)
    await g.grow(held, 800_000)
    await g.release(held)
    assert not record_present(path), "precondition: the release should have reaped it"

    returned = await g.grow(held, 900_000)

    assert returned == 0, (
        f"grow on a released lease returned {returned}, not 0: it thinks it still holds a booking"
    )
    assert g.status()["inflight_tokens"] == 0, (
        f"this process counts {g.status()['inflight_tokens']} in-flight tokens after "
        "growing a lease it had already released"
    )
    assert not record_present(path), (
        "growing a released lease brought its record back into the shared file"
    )


async def test_a_second_admission_over_the_same_file_counts_the_grown_tokens(
    tmp_path: Path,
):
    """Growth must reach the file, or the other editor window sees the opening figure.

    Two processes are two `Admission` instances over one file (ADR-0040). A run that
    grows in one of them has to be visible in the other's cross-process totals, or that
    window admits against a pool the first has already spent.
    """
    path = tmp_path / "shared.json"
    other_slots = slots_at(path)
    first = Admission(cfg(kv_token_budget=1_000_000), slots_at(path))
    other = Admission(cfg(kv_token_budget=1_000_000), other_slots)
    held = await take(first, 100_000)

    await first.grow(held, 800_000)

    report = await cross_process_status(other_slots, "")
    assert report["active"] is True
    assert other.status()["inflight_tokens"] == 0, (
        "precondition: the second instance holds nothing of its own, so the figure "
        "below can only have come from the shared file"
    )
    assert report["inflight_tokens"] == 800_000, (
        f"the other instance's cross-process view reports "
        f"{report['inflight_tokens']} tokens, not the 800,000 the lease grew to"
    )

    await first.release(held)
    report = await cross_process_status(other_slots, "")
    assert report["inflight_tokens"] == 0, (
        f"after release the machine-wide total is {report['inflight_tokens']}, not 0"
    )


async def test_a_growth_the_file_missed_gives_the_file_back_only_what_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A growth that never reached the file must not be subtracted from it on release.

    The file write is best effort, so a growth can land locally and miss the file. If the
    release then subtracted the grown figure there, it would take the difference out of
    this process's *other* leases, and the machine would under-count what is held -- the
    very error the growth exists to stop.
    """
    from claude_delegate_local.slots import SlotsUnavailable

    path = tmp_path / "shared.json"
    slots = slots_at(path)
    g = Admission(cfg(kv_token_budget=5_000_000), slots)
    grown = await take(g, 100_000)
    other = await take(g, 300_000)

    async def unreachable(**_kw):
        raise SlotsUnavailable("the file is gone for a moment")

    monkeypatch.setattr(slots, "grow", unreachable)
    await g.grow(grown, 800_000)
    # Precondition: the growth counted locally and missed the file.
    assert g.status()["inflight_tokens"] == 1_100_000
    assert booked_in_file(path) == 400_000

    await g.release(grown)

    assert booked_in_file(path) == 300_000, (
        f"the file books {booked_in_file(path)} tokens for the one lease still held, "
        "300,000: the release gave back a growth the file never recorded"
    )
    assert g.status()["inflight_tokens"] == 300_000
    await g.release(other)
