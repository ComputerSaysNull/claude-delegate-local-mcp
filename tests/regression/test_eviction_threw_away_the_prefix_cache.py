"""Evicting a tool result rewrote the history mid-stream, and the prefix cache paid.

Measured 2026-09-05, on an idle cluster, with `preemptions: 0` reported throughout so
nothing upstream was dropping anything. An eleven-turn delegation walked one file in ten
`read_file` calls. The backend's reported cache hit climbed exactly as an append-only
history should -- 75.0%, 80.0%, 83.3% on turns 5, 6, 7 -- and then, on the turn
`tool_results_evicted` first went to 1, fell to **0.0%** and stayed there for the rest of
the run. Turns 8-11 re-prefilled 315,625 tokens that turns 1-7 had already established.

The cause was not the eviction *policy*: without it the history grows without bound. It was
that the boundary was **recomputed every turn** -- "everything older than the newest `keep`"
moves forward by one each turn -- while the serving stack caches *prefixes*. Moving the
first difference toward the front discards everything after it.

ADR-0056 fixed it with two changes, and this file exists because measurement showed one
alone is not enough. Modelled over the same alternation the loop appends:

    today (per-turn boundary)          2.9% reusable, 409,588 chars re-prefilled
    pressure gate only                 5.1%           273,499
    pressure gate + sticky boundary   79.2%           109,310
    no eviction at all                93.0%            68,049   -- but unbounded

Gating alone is nearly worthless once pressure exists, because the boundary still moves.
The stickiness is the load-bearing half; the gate is what makes the common case free, since
the old policy fired at 7% of a 1M-token window where there was nothing to relieve.

No cluster is involved. Prefix stability is a property of the message list, so it is
provable without dispatching anything. The modelled figures above reproduced the cluster's
measured hit rates to the decimal on the turns before the first eviction, which is the
instrument being right rather than a coincidence.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.backends.base import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.loop import _OverflowGuard, stub_oldest_tool_results
from claude_delegate_local.registry import ModelEntry

KEEP = 6

# Big enough that a stub is a large change and a real result dominates the history, which
# is the case the cache actually meets. A tool result here is a file read.
RESULT_CHARS = 4000
TURNS = 24


def _cfg(**over) -> Config:
    kw = {"workspace_roots": (".",), "keep_tool_results": KEEP}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def _entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": "http://example.com:8000",
          "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


def _history(turns: int) -> tuple[Message, ...]:
    """The history as `run_agentic_loop` accumulates it, after `turns` completed turns."""
    messages: list[Message] = [Message("user", (TextBlock("audit these documents"),))]
    for n in range(1, turns + 1):
        messages.append(
            Message("assistant", (ToolUseBlock(id=f"call_{n}", name="read_file",
                                               input={"path": f"/doc/{n}.md"}),))
        )
        messages.append(
            Message("user", (ToolResultBlock(tool_use_id=f"call_{n}",
                                             content=f"{n}:" + "x" * RESULT_CHARS),))
        )
    return tuple(messages)


def _chars(messages: tuple[Message, ...]) -> int:
    return sum(
        sum(len(getattr(b, "content", getattr(b, "text", ""))) for b in m.content)
        for m in messages
    )


def _shared_prefix_chars(a: tuple[Message, ...], b: tuple[Message, ...]) -> int:
    """Characters of history the two requests share before the first difference.

    A stand-in for what the serving stack can reuse: it matches from the front and stops at
    the first mismatch, so leading messages that are byte-identical are reusable and
    everything past the first divergence is not.
    """
    total = 0
    # strict=False on purpose: consecutive turns differ in length by design, and the
    # shorter one is exactly where the comparison should stop.
    for left, right in zip(a, b, strict=False):
        if left != right:
            break
        total += sum(len(getattr(x, "content", getattr(x, "text", ""))) for x in left.content)
    return total


def _guard(*, under_pressure: bool, armed: bool = True) -> _OverflowGuard:
    """A guard whose pressure reading is fixed, so a test names its own case.

    `share()` is a projection over token counts the backend reports; driving it from real
    counts here would make these tests depend on the estimator rather than on the eviction
    boundary, which is what they are about.
    """
    guard = _OverflowGuard(_cfg(context_overflow_enabled=armed), _entry())
    guard.share = lambda: 0.9 if under_pressure else 0.05  # type: ignore[method-assign]
    return guard


def _walk(*, under_pressure: bool, armed: bool = True) -> list[float]:
    """Reusable share of each turn's prompt against the turn before it.

    One guard for the whole walk, because the boundary it carries between turns is the
    property under test. Rebuilding it per turn would test the old behaviour.
    """
    guard = _guard(under_pressure=under_pressure, armed=armed)
    shares: list[float] = []
    previous = stub_oldest_tool_results(_history(KEEP), guard.evict_upto(KEEP))[0]
    for turn in range(KEEP + 1, TURNS + 1):
        current = _history(turn)
        current = stub_oldest_tool_results(current, guard.evict_upto(turn))[0]
        shares.append(_shared_prefix_chars(previous, current) / _chars(current))
        previous = current
    return shares


def test_an_append_only_history_keeps_its_prefix() -> None:
    """The control. Without eviction every turn is a pure append, so the prefix holds.

    This is what the cluster measured on turns 1-7 before the first eviction. If it ever
    fails, the measurement is broken and every other test here proves nothing.
    """
    for turn in range(KEEP + 2, TURNS + 1):
        previous, current = _history(turn - 1), _history(turn)
        fraction = _shared_prefix_chars(previous, current) / _chars(current)
        assert fraction > 0.80, (
            f"turn {turn}: an append-only history shared only {fraction:.1%} of its prompt "
            "with the turn before it, so this file can no longer tell a rewritten history "
            "from an appended one."
        )


def test_a_history_with_room_left_is_never_rewritten() -> None:
    """The common case, and the half of the fix that makes it free.

    The old policy evicted unconditionally, at a prompt occupying about 7% of a 1M-token
    window -- there was no pressure to relieve and it paid a full re-prefill to relieve it.
    """
    assert min(_walk(under_pressure=False)) > 0.80


def test_under_pressure_the_boundary_moves_in_steps_rather_than_every_turn() -> None:
    """The load-bearing half. Gating alone measured 5.1%, barely above the 2.9% it replaced.

    The bar is deliberately generous: eviction is not asked to be free, only to leave the
    majority of the prompt reusable. Before the fix the measured share was 2.9% on this
    exact walk, and the backend reported a literal 0.0% hit on the live run this file is
    named for.
    """
    shares = _walk(under_pressure=True)
    mean = sum(shares) / len(shares)
    assert mean > 0.50, (
        f"mean reusable share {mean:.1%} across {len(shares)} turns. Eviction is moving the "
        "divergence point toward the front of the history faster than one step per `keep` "
        "turns, so the backend pays a cold prefill for the tail on most turns."
    )


def test_an_unarmed_guard_still_bounds_the_history() -> None:
    """The regression this fix nearly introduced, so it is asserted rather than assumed.

    `context_overflow_enabled` is off by default. Gating the whole policy on it would have
    left the default configuration never evicting at all -- trading a cache bug for an
    unbounded history, which is worse. Stepping is therefore unconditional, and this is the
    test that says so.
    """
    guard = _guard(under_pressure=False, armed=False)
    assert guard.evict_upto(TURNS) > 0

    # And it is still stepped rather than per-turn, which is the other half.
    shares = _walk(under_pressure=False, armed=False)
    assert sum(shares) / len(shares) > 0.50


def test_the_boundary_never_retreats() -> None:
    """Un-stubbing rewrites the history too, in the other direction, at the same cost."""
    guard = _guard(under_pressure=True)
    seen = [guard.evict_upto(n) for n in range(1, TURNS + 1)]
    assert seen == sorted(seen), seen

    # And it holds where it is when the pressure goes away, rather than snapping back.
    guard.share = lambda: 0.05  # type: ignore[method-assign]
    assert guard.evict_upto(TURNS) == seen[-1]


def test_a_stub_is_not_re_evicted_or_re_counted() -> None:
    """The count is work performed this turn, which is what the ledger reports."""
    guard = _guard(under_pressure=True)
    history = _history(TURNS)
    once, first = stub_oldest_tool_results(history, guard.evict_upto(TURNS))
    twice, second = stub_oldest_tool_results(once, guard.evict_upto(TURNS))

    assert first > 0, "the walk never evicted anything, so this proves nothing"
    assert second == 0
    assert twice == once


@pytest.mark.parametrize("upto", [0, -1])
def test_no_boundary_means_no_rewrite(upto: int) -> None:
    history = _history(TURNS)
    assert stub_oldest_tool_results(history, upto) == (history, 0)
