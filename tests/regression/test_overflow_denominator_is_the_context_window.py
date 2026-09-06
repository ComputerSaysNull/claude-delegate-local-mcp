"""A threshold computed against this server's own evictions, rather than the model's window.

Upstream lost real work to four bugs of one shape: a threshold over the wrong denominator.
The realistic local form is the nastiest of them, because it looks correct in isolation --
the loop already counts how many tool results it evicted, that number climbs exactly when a
delegation is getting long, and reaching for it instead of the model's context window
produces a detector that fires hardest on healthy delegations doing bulk work.

That is worth a regression test in its own file rather than a unit assertion, because the
counter it must not read is one the loop maintains a few lines away, and the next person to
touch this code will see it there.

Both directions, per the project's convention and the module docstring of
`tests/test_context_overflow.py`: a delegation with enormous headroom and heavy eviction must
stay silent, and one genuinely short of room must abort. A detector wired to the wrong number
passes exactly one of those.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import OVERFLOW_TIGHTEN_AT, Config
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist

# Big enough that every tool result but one is evicted on every turn, so `evicted` climbs
# with each turn while the model's actual window stays almost empty.
RESULT = "x" * 20_000

# What the backend reports the prompt costing, turn by turn. It has to *grow*, and that is
# not a detail of the fixture: a scripted backend returning one constant is indistinguishable
# from a backend silently truncating, and the retroactive check correctly aborts on it. This
# sequence is a healthy delegation -- rising steadily, and never near a million-token window.
GROWTH = (900, 5_000, 9_000, 13_000, 17_000, 21_000)

# The same shape, moved up into the band where eviction happens at all. ADR-0056 stopped
# evicting a history with room to spare, so the original confound -- heavy eviction against
# a barely-touched window -- can no longer be built: when armed, eviction and vast headroom
# are now mutually exclusive by construction. The confound did not disappear with it, it
# narrowed. Between OVERFLOW_EVICT_AT and OVERFLOW_TIGHTEN_AT the loop evicts every turn and
# must still not tighten, nudge or abort, and a detector reading `evicted` fires right here.
# Against a 40,000-token window these are 0.525 to 0.675 projected, evicting throughout and
# comfortably below the 0.70 that is allowed to act.
EVICTING_BUT_FINE = (420000,436000,452000,468000,484000,500000)
EVICTING_WINDOW = 800000_000


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "context_overflow_enabled": True,
        "keep_tool_results": 1,  # evict aggressively: this is the confound under test
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def reply(*, calls: bool, input_tokens: int) -> CanonicalResponse:
    content = (
        (TextBlock("working"), ToolUseBlock(id="c", name="big", input={"n": input_tokens}))
        if calls
        else (TextBlock("done"),)
    )
    return CanonicalResponse(
        content=content,
        finish_reason="tool_calls" if calls else "stop",
        input_tokens=input_tokens,
        output_tokens=5,
        model="served-id-1",
    )


class Scripted:
    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)

    async def complete(self, request):
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


@pytest.fixture
def big_tool():
    """One tool returning a result large enough to force eviction every turn."""
    added = {
        "big": tools.RegisteredTool(
            spec=ToolSpec(name="big", description="big", input_schema={"type": "object"}),
            handler=lambda cfg_, args: RESULT,
            cacheable=False,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def run(*replies, window: int, **over):
    return asyncio.run(
        loop.run_agentic_loop(
            cfg(**over),
            ModelEntry(
                key="flash",
                base_url=HOST,
                served_model_id="served-id-1",
                context_window=window,
            ),
            Scripted(*replies),
            loop.Delegation("do the thing"),
            allowed=frozenset({"big"}),
            max_turns=6,
        )
    )


def test_the_detector_ignores_the_eviction_count_however_high_it_climbs():
    """The bug, asserted where it lives: on the guard, not through a whole delegation.

    ADR-0056 stopped the loop evicting a history that has room to spare, so the original
    form of this test -- six turns of heavy eviction against a barely-touched window -- can
    no longer be built at all. Eviction and vast headroom are now mutually exclusive by
    construction, and the test below asserts that new fact directly.

    The property this file exists for is unchanged and is not about the loop: the detector
    must decide on `context_window` and must not read the eviction counter the loop
    maintains a few lines away. So it is stated on the guard, where a counter set to an
    absurd value cannot be dismissed as unreachable. A detector wired to the wrong number
    tightens or aborts here; a correct one is silent, because 5% of the window is 5% of the
    window no matter how much was evicted to keep it there.
    """
    guard = loop._OverflowGuard(cfg(), ModelEntry(
        key="flash", base_url=HOST, served_model_id="served-id-1",
        context_window=1_000_000,
    ))
    guard.prev_input_tokens = 20_000  # 2% of the window: nowhere near anything
    guard.evicted_upto = 10_000       # absurd on purpose, and must not be consulted
    ledger: dict = {}

    for turn in range(1, 7):
        guard.begin_turn(turn=turn, turns=6, ledger=ledger)  # must not raise

    assert guard.tightened_at == 0
    assert guard.nudged_at == 0
    assert guard.share() < OVERFLOW_TIGHTEN_AT


def test_a_history_with_room_to_spare_is_no_longer_evicted_at_all(big_tool):
    """Why the test above had to move, asserted rather than asserted-in-a-docstring.

    The original numbers are kept exactly -- a prompt climbing to 21K against a
    million-token window, about two percent -- and what they now prove is that the loop
    leaves such a history alone. Before ADR-0056 this same run evicted on nearly every turn.
    """
    result = run(
        *[reply(calls=True, input_tokens=n) for n in GROWTH[:5]],
        reply(calls=False, input_tokens=GROWTH[5]),
        window=1_000_000,
    )
    assert result.evicted == 0
    assert result.turns == 6
    assert result.overflow_tightened_at == 0
    assert result.overflow_nudged_at == 0


def test_the_same_eviction_pattern_against_a_small_window_does_abort(big_tool):
    """The other direction. Identical eviction, identical turn count, identical everything
    except the one number that is allowed to decide -- and now it must fire.

    Without this, a detector that had simply been switched off would pass the test above.
    """
    with pytest.raises(loop.ContextOverflowAborted) as caught:
        run(
            *[reply(calls=True, input_tokens=n) for n in GROWTH[:5]],
            reply(calls=False, input_tokens=GROWTH[5]),
            window=1000,
        )
    assert "abort threshold" in str(caught.value)
    assert caught.value.report["stopped_on_turn"] >= 2


def test_the_abort_report_names_what_the_server_ran_not_what_the_model_claimed(big_tool):
    """The report exists to be reconciled against the tree, so it must carry the ledger."""
    with pytest.raises(loop.ContextOverflowAborted) as caught:
        run(
            *[reply(calls=True, input_tokens=n) for n in GROWTH[:5]],
            reply(calls=False, input_tokens=GROWTH[5]),
            window=1000,
        )
    report = caught.value.report
    assert report["tool_calls_run"] >= 1
    assert set(report) >= {
        "stopped_on_turn",
        "of_turns",
        "projected_context_use",
        "tool_calls_run",
        "files_written",
        "writes_that_failed",
        "git_status",
    }
