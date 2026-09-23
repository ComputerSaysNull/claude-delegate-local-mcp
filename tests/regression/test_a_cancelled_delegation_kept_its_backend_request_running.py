"""A cancelled delegation kept its backend request running.

`_until_deadline` wraps the backend call in a task and waits on it. It cancelled that
task when its own deadline expired, but not when the *caller* was cancelled -- an MCP
`notifications/cancelled`, a client disconnect. The `CancelledError` left the wait, the
inner task kept streaming, and the HTTP stream was never closed, so the engine never saw
a disconnect: measured in #274 as 281s of generation after a cancel, while admission had
already released the lease and counted the cluster idle.

Both waiting shapes are covered, because they are two code paths: the default waits on
the task itself, and the test seam `tick_sleep` polls it.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local.loop import _until_deadline


async def _run(tick_sleep):
    inner_saw_cancel = asyncio.Event()
    started = asyncio.Event()

    async def backend_call():
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            inner_saw_cancel.set()
            raise
        return "finished"

    outer = asyncio.ensure_future(
        _until_deadline(backend_call(), lambda: None, tick=0.05, tick_sleep=tick_sleep)
    )
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    # Give an orphaned task every chance to show itself before asserting.
    await asyncio.sleep(0.05)
    return inner_saw_cancel.is_set()


def test_cancelling_the_caller_cancels_the_backend_call():
    assert asyncio.run(_run(None)), "the backend call kept running after its caller was cancelled"


def test_cancelling_the_caller_cancels_the_backend_call_under_the_test_seam():
    assert asyncio.run(_run(asyncio.sleep)), "the backend call kept running under tick_sleep"


def test_a_normal_answer_is_unchanged():
    async def quick():
        return 42

    assert asyncio.run(_until_deadline(quick(), lambda: None, tick=0.05)) == 42
