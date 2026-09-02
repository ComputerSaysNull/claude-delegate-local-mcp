"""Context economics: the reserve, the denominator, the plateau, and the graduated response.

Both directions everywhere, per this project's convention and for a specific reason here.
Four of the five bugs this feature exists to avoid were *false* thresholds -- checks that
fired when nothing was wrong -- so a suite that only proved the detector can fire would have
passed against every one of them. Each check below is paired with the closest scenario that
is not a violation.

The pure functions are exercised directly rather than through a delegation wherever that is
possible. A threshold reachable only by scripting eight turns is a threshold nobody will
re-test when they change it.
"""

from __future__ import annotations

import inspect

import pytest

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import (
    OVERFLOW_ABORT_AT,
    OVERFLOW_NUDGE_AT,
    OVERFLOW_TIGHTEN_AT,
    Config,
)
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


@pytest.fixture
def registered():
    """One cacheable tool, in the real registry for the duration of a test.

    Defined here rather than imported from `test_agentic_loop`: `tests/` is not a package,
    so a cross-module import does not resolve, and coupling two test modules to share three
    lines would be the wrong trade even where it did.
    """
    added = {
        "echo": tools.RegisteredTool(
            spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object"}),
            handler=lambda cfg_, args: f"echoed {args.get('what', '')}",
            cacheable=True,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)



def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",), "context_overflow_enabled": True}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


# --- the reserve is a fraction of the window ----------------------------------------------


def test_the_reserve_scales_with_the_window():
    """A tenth of a big window and a tenth of a small one are different numbers."""
    c = cfg(overflow_reserve_fraction=0.1)
    assert loop.overflow_reserve(c, entry(context_window=1_000_000)) == 100_000
    assert loop.overflow_reserve(c, entry(context_window=8192)) == 819


def test_the_reserve_is_never_one_fixed_number_across_models():
    """The other direction, and the shape of upstream's bug.

    A flat reserve returns the same count for both, so one constant prudent for a 1M window
    is most of an 8K one.
    """
    c = cfg()
    big = loop.overflow_reserve(c, entry(context_window=1_000_000))
    small = loop.overflow_reserve(c, entry(context_window=8192))
    assert big != small
    assert big / small == pytest.approx(1_000_000 / 8192, rel=1e-3)


# --- one branch decides which number is the window ----------------------------------------


def test_only_the_context_window_moves_the_projection():
    """Upstream's fifth test, in the form that is applicable here.

    A two-tier local/cloud fixture was evaluated on 2026-08-27 and rejected: there is no
    second branch to break, so such a test would pass whether or not the code had the flaw.
    Its lesson -- only one branch decides which number is the window -- is asserted instead
    as invariance. Every other number a wrong implementation might reach for is varied while
    `context_window` is held fixed, and the projection must not move.
    """
    e = entry(context_window=100_000)
    baseline = loop.projected_fraction(cfg(), e, 10_000, 500)
    for over in (
        {"thinking_max_tokens_floor": 999},
        {"max_turns_default": 3},
        {"keep_tool_results": 1},
        {"max_file_tokens": 5},
        {"max_total_prefetch_tokens": 50_000},
    ):
        assert loop.projected_fraction(cfg(**over), e, 10_000, 500) == baseline, over


def test_the_projection_moves_with_the_window_in_exact_proportion():
    """The other direction, and stronger than asserting that it differs.

    Above the reserve the projection is a share of the window, so halving the window must
    exactly double it. A ratio identifies *which* field is read; an inequality would pass
    for an implementation reading any field that happened to vary.
    """
    c = cfg(overflow_reserve_fraction=0.05)
    used, pending = 10_000, 500
    a = loop.projected_fraction(c, entry(context_window=100_000), used, pending) - 0.05
    b = loop.projected_fraction(c, entry(context_window=50_000), used, pending) - 0.05
    assert b / a == pytest.approx(2.0)


def test_an_entry_that_omits_the_window_still_reads_the_resolved_field():
    """The silent-default trap, asserted rather than left implicit.

    A registry entry omitting `context_window` inherits a default with no warning, which is
    the local form of upstream's architecture-maximum bug. The projection must read the
    resolved dataclass field, so an omitting entry and one naming that same number agree.
    """
    default = ModelEntry(key="k", base_url=HOST, served_model_id="s").context_window
    silent = loop.projected_fraction(cfg(), entry(), 1000, 0)
    explicit = loop.projected_fraction(cfg(), entry(context_window=default), 1000, 0)
    assert silent == explicit


# --- the plateau, and what explains it ----------------------------------------------------


def plateau(**over) -> bool:
    kw = {
        "prev_input_tokens": 10_000,
        "this_input_tokens": 10_000,
        "grew_by": 5_000,
        "evicted_this_turn": 0,
    }
    kw.update(over)
    return loop.plateaued_without_eviction(cfg(), **kw)


def test_a_prompt_that_stopped_growing_while_we_added_content_is_a_plateau():
    assert plateau() is True


def test_our_own_eviction_explains_a_plateau_and_silences_it():
    """The single most important negative case.

    Eviction is by far the likeliest reason a prompt stops growing, and it is something this
    server did. A check that did not subtract it first would fire on healthy delegations
    constantly, and be switched off within a day.
    """
    assert plateau(evicted_this_turn=1) is False


def test_the_explanation_is_a_count_this_server_set_not_anything_the_model_said():
    """ADR-0007, asserted structurally: the detector's signature admits no model text.

    There is no `finish_reason` here and no response to read one from. The only explanatory
    input is an integer the loop recorded at the point it evicted.
    """
    params = set(inspect.signature(loop.plateaued_without_eviction).parameters)
    assert params == {
        "cfg",
        "prev_input_tokens",
        "this_input_tokens",
        "grew_by",
        "evicted_this_turn",
    }


def test_a_turn_that_added_almost_nothing_is_not_evidence_of_anything():
    """Most turns add little, and a plateau under an empty append means nothing happened."""
    assert plateau(grew_by=1) is False
    assert plateau(grew_by=cfg().overflow_min_growth_tokens) is False


def test_growth_one_token_over_the_floor_is_evidence():
    """The other direction, so the floor is a threshold rather than a wall."""
    assert plateau(grew_by=cfg().overflow_min_growth_tokens + 1) is True


def test_a_backend_trimming_a_token_between_turns_does_not_read_as_truncation():
    """The slop. Without it, ordinary jitter aborts a healthy delegation."""
    slop = cfg().overflow_plateau_slop_tokens
    assert plateau(this_input_tokens=10_000 + slop) is True  # inside the slop: still flat
    assert plateau(this_input_tokens=10_000 + slop + 1) is False  # real growth


def test_real_growth_is_not_a_plateau():
    assert plateau(this_input_tokens=15_000) is False


# --- the guard, and the fact that it is inert by default -----------------------------------


def test_the_guard_does_nothing_at_all_when_the_feature_is_off():
    """Off by default means off: no tightening, no nudge and no abort, at any usage."""
    guard = loop._OverflowGuard(
        cfg(context_overflow_enabled=False), entry(context_window=1000)
    )
    guard.prev_input_tokens = 10_000  # far past every threshold
    guard.begin_turn(turn=2, turns=5, ledger=[])  # must not raise
    assert guard.nudge_due(turn=2) is False
    assert guard.keep == cfg().keep_tool_results
    assert guard.tightened_at == 0


def test_the_guard_tightens_retention_once_and_never_relaxes():
    c = cfg(keep_tool_results=6, overflow_tightened_keep_tool_results=1)
    guard = loop._OverflowGuard(c, entry(context_window=1000))
    guard.prev_input_tokens = int(OVERFLOW_TIGHTEN_AT * 1000)
    guard.begin_turn(turn=2, turns=9, ledger=[])
    assert guard.keep == 1
    assert guard.tightened_at == 2

    guard.prev_input_tokens = 0  # headroom won back
    guard.begin_turn(turn=3, turns=9, ledger=[])
    assert guard.keep == 1, "retention relaxed, so the same climb would simply repeat"
    assert guard.tightened_at == 2, "the turn it first tightened on must not move"


def test_the_guard_leaves_retention_alone_below_the_threshold():
    """The other direction. Tightening unconditionally would pass the test above."""
    guard = loop._OverflowGuard(cfg(keep_tool_results=6), entry(context_window=1000))
    guard.prev_input_tokens = 100
    guard.begin_turn(turn=2, turns=9, ledger=[])
    assert guard.keep == 6
    assert guard.tightened_at == 0


def test_the_guard_aborts_at_the_top_threshold_and_carries_a_report():
    guard = loop._OverflowGuard(cfg(), entry(context_window=1000))
    guard.prev_input_tokens = int(OVERFLOW_ABORT_AT * 1000)
    with pytest.raises(loop.ContextOverflowAborted) as caught:
        guard.begin_turn(turn=4, turns=9, ledger=[("read_file", "/a.py", False)])
    assert caught.value.report["stopped_on_turn"] == 4
    assert caught.value.report["tool_calls_run"] == 1


def test_the_guard_does_not_abort_just_below_the_top_threshold():
    """The other direction, and the one that matters most: an abort discards real work."""
    guard = loop._OverflowGuard(
        cfg(overflow_reserve_fraction=0.01), entry(context_window=10_000)
    )
    guard.prev_input_tokens = int((OVERFLOW_ABORT_AT - 0.02) * 10_000)
    guard.begin_turn(turn=4, turns=9, ledger=[])  # must not raise


def test_the_nudge_is_due_above_its_threshold_and_not_below():
    guard = loop._OverflowGuard(
        cfg(overflow_reserve_fraction=0.01), entry(context_window=10_000)
    )
    guard.prev_input_tokens = int((OVERFLOW_NUDGE_AT - 0.10) * 10_000)
    assert guard.nudge_due(turn=2) is False
    assert guard.nudged_at == 0
    guard.prev_input_tokens = int((OVERFLOW_NUDGE_AT + 0.02) * 10_000)
    assert guard.nudge_due(turn=3) is True
    assert guard.nudged_at == 3


def test_the_thresholds_are_ordered():
    """A sanity check with teeth: an escalation whose stages are out of order would abort
    before it had ever nudged, and every scenario test above would still pass.
    """
    assert OVERFLOW_TIGHTEN_AT < OVERFLOW_NUDGE_AT < OVERFLOW_ABORT_AT


# --- a nudge reply concatenates, and never overwrites ---------------------------------------


def response(text: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


def test_a_short_acknowledgement_does_not_erase_what_was_already_said():
    kept = loop._preserve_across_nudge(
        response("Understood, wrapping up."), said_before="The bug is in paths.py line 40."
    )
    assert "The bug is in paths.py line 40." in kept.text
    assert "Understood, wrapping up." in kept.text


def test_an_ordinary_answer_is_returned_untouched():
    """The other direction. Concatenating always would make every multi-turn answer worse,
    because the earlier turns of a healthy delegation are narration rather than findings.
    """
    original = response("The bug is in paths.py line 40.")
    assert loop._preserve_across_nudge(original, said_before="") is original


def test_findings_the_model_restated_itself_are_not_repeated():
    said = "The bug is in paths.py line 40."
    kept = loop._preserve_across_nudge(response(f"{said} Done."), said_before=said)
    assert kept.text.count(said) == 1


# --- estimating what we appended ------------------------------------------------------------


def test_every_block_type_contributes_to_the_estimate():
    """A block type silently costed at zero would hold growth under the floor, and the
    plateau check would then never fire -- exactly the shape this project keeps finding.
    """
    c = cfg()
    for block in (
        TextBlock("x" * 4000),
        ThinkingBlock("x" * 4000),
        ToolResultBlock(tool_use_id="t", content="x" * 4000),
        ToolUseBlock(id="t", name="read_file", input={"path": "x" * 4000}),
    ):
        size = loop.estimate_message_tokens(c, Message("user", (block,)))
        assert size > 0, type(block).__name__


def test_an_empty_message_costs_nothing():
    assert loop.estimate_message_tokens(cfg(), Message("user", ())) == 0


# --- through the real loop -------------------------------------------------------------------
#
# The checks above exercise the guard directly, which is where the thresholds are decided.
# These few run a whole delegation, because the wiring is its own thing that can be wrong
# while every threshold is right: a nudge computed correctly and then never appended, or
# appended in place of the countdown, would pass everything above.


class Scripted:
    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


def calling(input_tokens: int, *, text: str = "working") -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text), ToolUseBlock(id="c", name="echo", input={"what": "x"})),
        finish_reason="tool_calls",
        input_tokens=input_tokens,
        output_tokens=5,
        model="served-id-1",
    )


def answering(input_tokens: int, text: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=input_tokens,
        output_tokens=5,
        model="served-id-1",
    )


def run(backend, *, window: int, **over):
    import asyncio

    return asyncio.run(
        loop.run_agentic_loop(
            cfg(**over),
            entry(context_window=window),
            backend,
            loop.Delegation("do the thing"),
            allowed=frozenset({"echo"}),
            max_turns=4,
        )
    )


def tail_text(request) -> str:
    """Every text block on the message carrying this turn's tool results."""
    return " ".join(
        b.text for b in request.messages[-1].content if isinstance(b, TextBlock)
    )


def test_the_wrap_up_line_joins_the_countdown_rather_than_replacing_it(registered):
    """Both lines, on the same message. They say different things -- one counts turns and
    the other counts room -- and a delegation can be short of either without the other.
    """
    # Sized so the nudge threshold is crossed at the end of turn one while the abort
    # threshold is still clear at the start of turn two -- the only window in which the
    # wrap-up line is actually observable on the wire.
    backend = Scripted(calling(8200), answering(8300, "the finding"))
    run(backend, window=10_000, overflow_reserve_fraction=0.05)
    tail = tail_text(backend.requests[-1])
    assert loop.WRAP_UP_LINE in tail
    assert "turns remain" in tail


def test_no_wrap_up_line_when_there_is_room(registered):
    """The other direction. A loop that always appended it would pass the test above."""
    backend = Scripted(calling(10), answering(12, "the finding"))
    run(backend, window=1_000_000)
    assert loop.WRAP_UP_LINE not in tail_text(backend.requests[-1])


def test_an_acknowledgement_to_a_nudge_does_not_lose_the_finding(registered):
    """End to end: the model says something substantial, is nudged, and replies briefly."""
    backend = Scripted(
        calling(8200, text="The bug is in paths.py line 40."),
        answering(8300, "Understood, wrapping up."),
    )
    result = run(backend, window=10_000, overflow_reserve_fraction=0.05)
    assert result.overflow_nudged_at == 1
    assert "The bug is in paths.py line 40." in result.response.text
    assert "Understood, wrapping up." in result.response.text


def test_an_unnudged_delegation_answers_with_its_final_turn_only(registered):
    """The other direction, and the behaviour that must not regress: the earlier turns of a
    healthy delegation are narration, and joining them onto every answer makes it worse.
    """
    backend = Scripted(
        calling(10, text="Let me check that file."),
        answering(12, "The bug is in paths.py line 40."),
    )
    result = run(backend, window=1_000_000)
    assert result.response.text == "The bug is in paths.py line 40."
    assert "Let me check that file." not in result.response.text


def test_retention_tightens_when_the_window_fills_and_is_reported(registered):
    backend = Scripted(calling(700), calling(720), answering(740, "done"))
    result = run(
        backend, window=1000, keep_tool_results=6,
        overflow_tightened_keep_tool_results=1, overflow_reserve_fraction=0.01,
    )
    assert result.overflow_tightened_at > 0


def test_retention_is_left_alone_when_there_is_room(registered):
    """The other direction, and it reports zero rather than a turn number."""
    backend = Scripted(calling(10), calling(12), answering(14, "done"))
    result = run(backend, window=1_000_000, keep_tool_results=6)
    assert result.overflow_tightened_at == 0


def test_a_delegation_with_the_feature_off_is_untouched_at_any_usage(registered):
    """The default path. Nothing above may fire when the switch is off."""
    backend = Scripted(calling(99_000), answering(99_500, "done"))
    result = run(backend, window=1000, context_overflow_enabled=False)
    assert result.overflow_nudged_at == 0
    assert result.overflow_tightened_at == 0
    assert result.response.text == "done"
    assert loop.WRAP_UP_LINE not in tail_text(backend.requests[-1])


# ---- what the mismatch report says, and to whom ---------------------------------------


class _WindowEndpoint:
    """Answers the window probe with a number, and nothing else."""

    def __init__(self, reported: int) -> None:
        self.reported = reported

    async def probe_window(self):
        return self.reported

    async def complete(self, request):
        raise AssertionError("not used")

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


def _verdict(entry: ModelEntry, reported: int) -> str:
    """The reason a mismatch gave for leaving overflow handling off."""
    import asyncio

    from claude_delegate_local import server

    check = server.WindowCheck(
        Config(workspace_roots=(".",), context_overflow_enabled=True),  # type: ignore[arg-type]
        clock=lambda: 1000.0,
    )
    _armed, reason = asyncio.run(check.armed(_WindowEndpoint(reported), entry))
    return reason


def test_a_mismatch_on_an_assumed_window_does_not_blame_the_operators_file():
    """It said "models.toml gives context_window=..." about a file that gives no such key.

    That sent someone to correct a line which was never there. When the number was
    assumed rather than stated, the endpoint has just reported the right one, so the
    remedy can name it outright.
    """
    entry = ModelEntry(
        key="flash", base_url=HOST, served_model_id="served-id-1",
        context_window_defaulted=True,
    )
    reason = _verdict(entry, reported=1_048_576)
    assert "sets no context_window" in reason
    assert "gives context_window" not in reason
    assert "Set context_window=1048576" in reason


def test_a_mismatch_on_a_stated_window_still_says_to_correct_it():
    """The other direction. A number the operator did state is theirs to fix, and the
    report must keep saying so rather than telling everyone they set nothing."""
    entry = ModelEntry(
        key="flash", base_url=HOST, served_model_id="served-id-1",
        context_window=200_000,
    )
    reason = _verdict(entry, reported=1_048_576)
    assert "gives context_window=200000" in reason
    assert "sets no context_window" not in reason
    assert "Correct models.toml" in reason
