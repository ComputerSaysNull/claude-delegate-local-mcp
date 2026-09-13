"""The heartbeat's callback is called with what its callers actually accept.

`_keepalive` catches `Exception` around the call and returns. That is deliberate and its
docstring says why: a heartbeat exists to stop a long delegation being abandoned, and one
that killed a delegation because a notification could not be delivered would be strictly
worse than not having it. The trade is sound and is not what these tests change.

What the trade costs is that a **`TypeError` from an arity mismatch is indistinguishable
from a delivery failure**, so changing `on_alive`'s shape and missing one caller stops the
heartbeat silently -- and the symptom is an empty list of beats, not a stack trace. So the
shape is asserted here instead, where a mismatch is a red test rather than a quiet outage.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from claude_delegate_local import loop, server
from claude_delegate_local.config import Config

pytestmark = pytest.mark.anyio

# Every place `on_alive` is declared. `_keepalive` is the one that *calls* it; the other
# three only pass it along, so a shape that drifts between them is the bug this catches.
DECLARING = (
    loop._keepalive,
    loop.run_one_shot,
    loop.run_agentic_loop,
    server.dispatch_delegation,
)


def _declared(fn) -> str:
    """The `on_alive` annotation as written, stripped of the optional wrapper."""
    raw = str(inspect.signature(fn).parameters["on_alive"].annotation)
    return raw.replace(" | None", "").replace("'", "").strip()


def test_every_declaration_of_the_heartbeat_callback_has_the_same_shape():
    """A drift check, because the mismatch it guards against cannot raise where it happens."""
    shapes = {fn.__name__: _declared(fn) for fn in DECLARING}
    assert len(set(shapes.values())) == 1, (
        f"on_alive is declared with more than one shape: {shapes}. _keepalive swallows the "
        "TypeError a mismatch raises, so the heartbeat would simply stop."
    )


async def _beats(callback, *, interval: int = 1) -> int:
    """How many times `_keepalive` managed to call this callback in just over one interval."""
    task = asyncio.create_task(
        loop._keepalive(
            Config(workspace_roots=(".",), keepalive_interval=interval),
            callback, lambda: 0.0, lambda: 10.0,
        )
    )
    await asyncio.sleep(interval + 0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return 0


async def test_a_mismatched_callback_beats_nothing_where_a_correct_one_beats():
    """Both halves, because either alone is a check that cannot fail.

    Asserting only that the wrong callback is silent passes against a heartbeat that never
    fires at all -- including one whose interval the test simply did not wait out. The
    correct callback is the control that proves the window was long enough.
    """
    good: list[tuple] = []
    bad: list[tuple] = []

    async def correct(elapsed, of_seconds, ends_in, chunks, since) -> None:
        good.append((elapsed, of_seconds, ends_in, chunks, since))

    async def too_few(_only_one) -> None:  # the shape a missed call site leaves behind
        bad.append(("wrong",))

    await _beats(correct)
    await _beats(too_few)

    assert good, "the window must be long enough for a correct callback to beat"
    assert not bad, "a mismatched callback must be silent -- and that is the danger"
