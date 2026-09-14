"""Eviction counted results, so a one-line refusal cost what a 50KB file cost.

`keep_tool_results` was a count. One measured run held 36 results spanning 200 bytes to
50,068, which is a 250x spread priced identically -- `config.py`'s own description said so
and filed it rather than fixing it. What fills a context window is bytes, so bytes are what
the boundary is now driven by (ADR-0079).

Two things must survive the change, and each has a test here that fails if it does not.
Selection stays oldest-first, because the prompt is cached by prefix and lifting a large
result out of the middle would invalidate everything after it (ADR-0056) -- only *where the
cut falls* is driven by size. And `keep_tool_results` stays a floor, so the newest results,
which are the ones the model is working from, are never stubbed however large they are.
"""

from __future__ import annotations

from dataclasses import replace

from claude_delegate_local.config import Config
from claude_delegate_local.loop import _OverflowGuard, _tool_result_sizes
from claude_delegate_local.backends.base import Message, ToolResultBlock
from claude_delegate_local.registry import ModelEntry

KEEP = 4
BUDGET = 10_000


def _cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "keep_tool_results": KEEP,
        "retained_tool_result_tokens": BUDGET,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def _guard(cfg: Config | None = None) -> _OverflowGuard:
    cfg = cfg or _cfg()
    entry = ModelEntry(
        key="flash", base_url="http://example.com:8000", served_model_id="served-id-1",
    )
    # The shipped configuration, which is where the bug was measured: overflow handling
    # off, and a window the operator never declared. Both halves of the pressure gate are
    # then unavailable -- `armed` is False and `pressure_known` is False -- so it is
    # skipped and the size boundary alone decides. Declaring the window instead would hold
    # the boundary below `OVERFLOW_EVICT_AT` and every test here would measure the gate.
    guard = _OverflowGuard(cfg, replace(entry, context_window_defaulted=True))
    return guard


def _history(*sizes: int) -> tuple[Message, ...]:
    """One tool result per size, in the order given, oldest first."""
    return tuple(
        Message("user", (ToolResultBlock(tool_use_id=f"c{i}", content="x" * n),))
        for i, n in enumerate(sizes)
    )


def test_a_history_of_refusals_is_not_evicted():
    """The bug, stated from the cheap side.

    Twelve one-line refusals are three times the old count of 4 and would have been
    stubbed. They cost almost nothing, so nothing needs to go.
    """
    guard = _guard()
    history = _history(*([200] * 12))

    assert guard.evict_upto(_tool_result_sizes(guard.cfg, history)) == 0


def test_a_history_of_large_files_is_evicted():
    """The same count, from the expensive side, and the reason this is not just a loosening.

    Twelve 50KB results is the same number of results as above and vastly over budget, so
    the boundary must move. A change that only ever evicted less would pass the test above
    and fail this one.
    """
    guard = _guard()
    history = _history(*([50_068] * 12))

    assert guard.evict_upto(_tool_result_sizes(guard.cfg, history)) > 0


def test_the_cut_stays_oldest_first_when_the_big_result_is_recent():
    """Selection must not follow size, however tempting the arithmetic looks.

    One 50KB result sits at the newest end, behind a run of cheap ones. Dropping *it*
    would free the most tokens for the least stubs -- and would rewrite the newest part of
    the history, discarding the prefix cache from that point on and taking away what the
    model is currently working from. The boundary is an index from the front, so the most
    this may do is advance over the cheap results ahead of it.
    """
    guard = _guard()
    # Eleven cheap, then one huge: the expensive one is the last and is inside `keep`.
    history = _history(*([200] * 11), 50_068)

    upto = guard.evict_upto(_tool_result_sizes(guard.cfg, history))

    assert upto <= len(history) - KEEP, "the floor was breached"
    # Whatever it decided, it is a prefix of the history: the 50KB block is at index 11 and
    # the floor keeps the last KEEP, so it cannot be in the evicted range.
    assert upto < 11, "the newest, largest result was selected"


def test_the_floor_holds_when_every_result_is_enormous():
    """`keep` is a floor, not a target.

    Every result is far over budget on its own, so a purely size-driven boundary would
    stub the entire history and leave the model with nothing to answer from. The newest
    `keep` survive whatever they cost.
    """
    guard = _guard()
    history = _history(*([50_068] * 8))

    upto = guard.evict_upto(_tool_result_sizes(guard.cfg, history))

    assert upto == len(history) - KEEP


def test_the_boundary_still_steps_rather_than_creeping():
    """The property ADR-0056 bought, restated in the new unit.

    Rounding the size-driven cut *up* to a step looks more eager and is the bug: capped by
    the floor it advances by one every turn, which is the per-turn boundary that costs the
    prefix cache everything after it. Flooring keeps the jumps whole.
    """
    guard = _guard()
    seen = [
        guard.evict_upto(_tool_result_sizes(guard.cfg, _history(*([5_000] * n))))
        for n in range(1, 20)
    ]

    assert all(v % KEEP == 0 for v in seen), f"a boundary landed off-step: {seen}"
    assert seen == sorted(seen), "the boundary retreated"
