"""Cancelling the task that opened a burst wait cancelled every joiner, each holding a valid slot.

The opener's cancellation path stores its `CancelledError` in the shared wait future, so a
joiner that was never cancelled lands in its own `except asyncio.CancelledError` branch,
releases a slot it validly held, and is cancelled too.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 5,
        # Long enough that the opener is still inside the idle hold when the test cancels it.
        "admission_idle_hold": 5.0,
        "kv_token_budget": 100_000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    """Spin until `predicate` is true, or fail rather than hang."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met before deadline")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cancelling_the_bursts_opener_does_not_cancel_the_joiners() -> None:
    g = Admission(cfg())

    joiner_got: dict = {}
    joiner_release = asyncio.Event()

    async def opener() -> None:
        lease = await g.acquire(100, entry_key="flash", entry_limit=5)
        try:
            await asyncio.sleep(30)
        finally:
            await g.release(lease)

    async def joiner() -> None:
        lease = await g.acquire(100, entry_key="flash", entry_limit=5)
        joiner_got["lease"] = lease
        try:
            await joiner_release.wait()
        finally:
            await g.release(lease)

    opener_task = asyncio.create_task(opener())
    joiner_task: asyncio.Task | None = None
    try:
        # The gate is idle, so the opener opens the burst wait and holds it.
        await _wait_until(lambda: g._holding is not None)

        joiner_task = asyncio.create_task(joiner())
        # The joiner takes a slot of its own (two now in flight), then joins the opener's
        # open wait; give it the instant it needs to be parked on the shared future.
        await _wait_until(lambda: g.status()["inflight_seqs"] == 2)
        await asyncio.sleep(0.05)

        # Cancel only the opener, while it is inside the idle hold. The opener must end
        # cancelled -- that is the request we asked for.
        opener_task.cancel()
        opener_outcome = await asyncio.gather(opener_task, return_exceptions=True)
        assert isinstance(opener_outcome[0], asyncio.CancelledError)

        # The joiner was never cancelled, so it must finish its acquire and keep its slot.
        await _wait_until(
            lambda: joiner_task.cancelled() or "lease" in joiner_got, timeout=2.0
        )
        assert not joiner_task.cancelled(), (
            "the joiner was cancelled by the opener's cancellation"
        )
        assert "lease" in joiner_got, "the joiner never completed its acquire"
        # The opener released its slot on the way out; the joiner's is still held.
        assert g.status()["inflight_seqs"] == 1
    finally:
        # Give the joiner's lease back and clean up any task still running.
        joiner_release.set()
        for task in (opener_task, joiner_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (opener_task, joiner_task) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_a_joiner_cancelled_itself_still_gives_its_slot_back() -> None:
    """The other half: telling the two cancellations apart must not swallow the joiner's own."""
    g = Admission(cfg())

    async def member() -> None:
        lease = await g.acquire(100, entry_key="flash", entry_limit=5)
        try:
            await asyncio.sleep(30)
        finally:
            await g.release(lease)

    opener_task = asyncio.create_task(member())
    joiner_task: asyncio.Task | None = None
    try:
        await _wait_until(lambda: g._holding is not None)
        joiner_task = asyncio.create_task(member())
        await _wait_until(lambda: g.status()["inflight_seqs"] == 2)
        await asyncio.sleep(0.05)

        joiner_task.cancel()
        outcome = await asyncio.gather(joiner_task, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert g.status()["inflight_seqs"] == 1, "the cancelled joiner kept its slot"
        assert not opener_task.done(), "cancelling a joiner reached the opener"
    finally:
        for task in (opener_task, joiner_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (opener_task, joiner_task) if task is not None),
            return_exceptions=True,
        )
