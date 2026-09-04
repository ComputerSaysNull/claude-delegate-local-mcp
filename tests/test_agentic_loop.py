"""The turn loop: turns, eviction, dedup, countdown, and the final-turn short-circuit.

Every check here is asserted in both directions -- that it fires on a real violation, and
that it stays silent on the closest thing that is not one. A loop that never declares tools
passes a "the final turn declares none" test, and one that always declares them passes a
"the turn before it does" test; only the pair distinguishes a working short-circuit from
either broken one.

The tools are registered into the real `tools.REGISTRY` for the duration of a test rather
than mocked around, because both `allowed_tools` sites read that table and a test that
bypassed it would be testing a table of its own.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop, sandbox, tools
from claude_delegate_local.backends.base import (
    BashOutcome,
    CanonicalResponse,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    # The deadlines have to nest, and these tests set absurdly small ceilings on purpose so
    # a fake clock is cheap. Follow the ceiling down unless the test names this itself, so
    # shrinking `dispatch_timeout` does not silently become a test of the stall deadline.
    if "dispatch_timeout" in kw and "stall_timeout" not in kw:
        kw["stall_timeout"] = kw["dispatch_timeout"]
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


# --- scripted replies ---------------------------------------------------------------------


def says(text: str) -> CanonicalResponse:
    """A final answer: text, no tool calls, so the loop stops here."""
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


def wants(*calls: tuple[str, dict], text: str = "") -> CanonicalResponse:
    """A turn that calls tools. Ids are positional so a test can name them."""
    blocks: list = [TextBlock(text)] if text else []
    blocks += [
        ToolUseBlock(id=f"call-{i}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    ]
    return CanonicalResponse(
        content=tuple(blocks),
        finish_reason="tool_calls",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


def results_in(request) -> list[ToolResultBlock]:
    """The tool results the loop sent back, from the last message of a request."""
    return [b for b in request.messages[-1].content if isinstance(b, ToolResultBlock)]


class ScriptedTurns:
    """Returns one scripted reply per call, and records every request it was given."""

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


# --- test tools ---------------------------------------------------------------------------


@pytest.fixture
def registered():
    """Two tools in the real registry: one cacheable, one with side effects.

    Removed again afterwards. `REGISTRY` is one dict object shared by `tools.py` and the
    import in `loop.py`, so mutating it here is what both sites actually read.
    """
    ran: list[tuple[str, dict]] = []

    def echo(cfg_, args):
        ran.append(("echo", dict(args)))
        return f"echoed {args.get('what', '')}"

    def poke(cfg_, args):
        ran.append(("poke", dict(args)))
        return "poked"

    def refuse(cfg_, args):
        ran.append(("refuse", dict(args)))
        raise tools.ToolRefused("no.")

    added = {
        "echo": tools.RegisteredTool(
            spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object"}),
            handler=echo,
            cacheable=True,
        ),
        "poke": tools.RegisteredTool(
            spec=ToolSpec(name="poke", description="poke", input_schema={"type": "object"}),
            handler=poke,
        ),
        "refuse": tools.RegisteredTool(
            spec=ToolSpec(name="refuse", description="no", input_schema={"type": "object"}),
            handler=refuse,
            cacheable=True,
        ),
    }
    tools.REGISTRY.update(added)
    try:
        yield ran
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def run(backend, *, allowed=frozenset({"echo", "poke", "refuse"}), **over):
    kw = {"max_turns": 5}
    kw.update(over)
    return asyncio.run(
        loop.run_agentic_loop(
            kw.pop("cfg", cfg()),
            entry(),
            backend,
            loop.Delegation(kw.pop("task", "do the thing")),
            allowed=allowed,
            **kw,
        )
    )


# --- turns ---------------------------------------------------------------------------------


def test_a_tool_call_then_an_answer_is_two_turns(registered):
    backend = ScriptedTurns(wants(("echo", {"what": "hi"})), says("done"))
    result = run(backend)
    assert result.turns == 2
    assert result.response.text == "done"
    assert registered == [("echo", {"what": "hi"})]


def test_an_answer_on_the_first_turn_is_one_turn(registered):
    """The other direction: the loop must not spend turns it was not asked for."""
    backend = ScriptedTurns(says("done"))
    result = run(backend)
    assert result.turns == 1
    assert result.tool_calls == 0
    assert backend.replies == [], "exactly one dispatch"


def test_the_tool_result_goes_back_as_a_user_message(registered):
    backend = ScriptedTurns(wants(("echo", {"what": "hi"})), says("done"))
    run(backend)
    second = backend.requests[1]
    roles = [m.role for m in second.messages]
    assert roles == ["user", "assistant", "user"]
    results = results_in(second)
    assert len(results) == 1
    assert results[0].tool_use_id == "call-0"
    assert "echoed hi" in results[0].content


def test_the_turn_budget_is_clamped_to_the_hard_cap():
    capped = cfg(max_turns_default=3, max_turns_hard_cap=3)
    assert loop.resolve_max_turns(capped, 99) == 3


def test_a_budget_under_the_cap_is_left_alone():
    """The other direction: a clamp that returned the cap always would pass the test above."""
    capped = cfg(max_turns_default=3, max_turns_hard_cap=3)
    assert loop.resolve_max_turns(capped, 2) == 2


def test_a_turn_budget_below_one_is_refused():
    with pytest.raises(loop.InvalidDelegation, match="at least 1"):
        loop.resolve_max_turns(cfg(), 0)


def test_the_configured_default_is_used_when_the_caller_names_no_budget():
    assert loop.resolve_max_turns(cfg(max_turns_default=7)) == 7


# --- the final-turn short-circuit -----------------------------------------------------------


def test_the_final_turn_declares_no_tools(registered):
    backend = ScriptedTurns(wants(("echo", {})), wants(("echo", {})))
    result = run(backend, max_turns=2)
    assert backend.requests[-1].tools == (), "tools must be withdrawn on the last turn"
    assert result.hit_turn_limit is True


def test_the_turn_before_the_last_one_does_declare_them(registered):
    """The other direction. A loop that never declares tools would pass the test above."""
    backend = ScriptedTurns(wants(("echo", {})), wants(("echo", {})))
    run(backend, max_turns=2)
    names = [spec.name for spec in backend.requests[0].tools]
    assert "echo" in names and "poke" in names


def test_an_answer_given_freely_does_not_report_the_turn_limit(registered):
    """The answer must land *before* the last turn, or this tests nothing it claims.

    It used to answer on turn 2 of 2 -- the final turn, whose toolset is withdrawn -- which
    is the opposite of freely given, and it passed only because the flag then also required
    a tool call the backend could not make. Three turns, answering on the second.
    """
    backend = ScriptedTurns(wants(("echo", {})), says("done"))
    result = run(backend, max_turns=3)
    assert result.turns == 2
    assert result.hit_turn_limit is False


# --- allowed_tools, both sites --------------------------------------------------------------


def test_a_tool_outside_the_allowed_set_is_refused_even_when_the_model_calls_it(registered):
    """The declared list is advisory. This is the site that matters: the model was never
    offered `poke`, calls it anyway, and the handler must not run."""
    backend = ScriptedTurns(wants(("poke", {})), says("done"))
    result = run(backend, allowed=frozenset({"echo"}))
    assert registered == [], "the withheld tool's handler ran"
    assert result.tool_errors == 1
    refusal = results_in(backend.requests[1])[0]
    assert refusal.is_error and "not available" in refusal.content


def test_the_same_call_inside_the_allowed_set_runs(registered):
    """The other direction, in the same shape: only membership differs."""
    backend = ScriptedTurns(wants(("poke", {})), says("done"))
    result = run(backend, allowed=frozenset({"echo", "poke"}))
    assert registered == [("poke", {})]
    assert result.tool_errors == 0


def test_run_bash_is_declared_now_that_something_confines_it(monkeypatch):
    """M5's point. It was withheld from M4 until the sandbox and the mount-level denylist
    both existed; they do, so the route is open and the tool is offered.

    The host's own bubblewrap is stubbed out because that is a separate condition with its
    own test in `test_tools.py`; this one is about the withholding, and it should say the
    same thing on a machine that cannot run a sandbox at all.
    """
    monkeypatch.setattr(sandbox, "available", lambda _cfg: True)
    # Forced for the same reason `sandbox.available` is: otherwise the set below depends on
    # whether this host happens to have git, and the assertion stops being about tools.
    monkeypatch.setattr(tools, "git_available", lambda: True)
    c = cfg()
    assert "run_bash" in tools.available_tool_names(c)
    assert "run_bash" in {s.name for s in tools.declared_tools(tools.resolve_allowed(None, c))}
    assert tools.available_tool_names(c) == frozenset(
        {"read_file", "search_files", "read_git", "write_file", "edit_file", "run_bash"})


def test_withholding_still_works_although_nothing_is_withheld(monkeypatch):
    """`WITHHELD_TOOL_NAMES` is empty now and kept anyway, so this is what stops it rotting.

    An empty set makes every assertion about it pass for the wrong reason, and the next
    thing implemented before it is safe would find the mechanism quietly broken. Proven
    against a real entry instead: a withheld tool leaves the declared set, and a caller
    naming it explicitly still cannot widen the set back.
    """
    monkeypatch.setattr(tools, "WITHHELD_TOOL_NAMES", frozenset({"write_file"}))
    monkeypatch.setattr(sandbox, "available", lambda _cfg: True)
    monkeypatch.setattr(tools, "git_available", lambda: True)
    c = cfg()
    assert tools.available_tool_names(c) == frozenset(
        {"read_file", "search_files", "read_git", "edit_file", "run_bash"})
    assert tools.resolve_allowed(["write_file"], c) == frozenset()
    assert tools.resolve_allowed(["read_file", "write_file"], c) == frozenset({"read_file"})
    declared = {s.name for s in tools.declared_tools(tools.resolve_allowed(None, c))}
    assert "write_file" not in declared


def test_a_tool_outside_the_allowed_set_is_still_refused_at_the_execution_site():
    """Site two, which never consulted the withholding and so is unaffected by emptying it.
    A model can name a tool it was never offered, and that is the case worth catching."""
    call = ToolUseBlock(id="x", name="run_bash", input={"command": "ls"})
    result = tools.execute_tool(cfg(), call, frozenset({"read_file"}))
    assert result.is_error and "not available" in result.content


# --- dedup ------------------------------------------------------------------------------------


def test_an_identical_repeat_is_served_without_running_the_tool_again(registered):
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"})),
        wants(("echo", {"what": "a"})),
        says("done"),
    )
    result = run(backend)
    assert registered == [("echo", {"what": "a"})], "the tool ran twice"
    assert result.deduped == 1
    served = results_in(backend.requests[2])[0]
    assert served.content.startswith(loop.REPEAT_PREFIX)
    assert "echoed a" in served.content


def test_a_call_differing_by_one_byte_is_not_deduplicated(registered):
    """The other direction. Dedup that matched too widely would answer the wrong question."""
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"})),
        wants(("echo", {"what": "b"})),
        says("done"),
    )
    result = run(backend)
    assert registered == [("echo", {"what": "a"}), ("echo", {"what": "b"})]
    assert result.deduped == 0


def test_argument_order_alone_does_not_defeat_dedup(registered):
    backend = ScriptedTurns(
        wants(("echo", {"what": "a", "n": 1})),
        wants(("echo", {"n": 1, "what": "a"})),
        says("done"),
    )
    assert run(backend).deduped == 1


def test_a_side_effecting_tool_clears_what_was_cached_before_it(registered):
    """A read taken before a write and repeated after it has two different correct answers.
    Serving the first one twice would hand the model a file from before its own overwrite."""
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"})),
        wants(("poke", {})),
        wants(("echo", {"what": "a"})),
        says("done"),
    )
    result = run(backend)
    assert registered == [
        ("echo", {"what": "a"}),
        ("poke", {}),
        ("echo", {"what": "a"}),
    ], "the read was served from a cache the write should have emptied"
    assert result.deduped == 0


def test_a_refusal_is_not_cached(registered):
    """Several refusals are transient -- a file that does not exist yet is the obvious one.
    Caching one would make it permanent for the rest of the delegation."""
    backend = ScriptedTurns(
        wants(("refuse", {})),
        wants(("refuse", {})),
        says("done"),
    )
    result = run(backend)
    assert registered == [("refuse", {}), ("refuse", {})]
    assert result.deduped == 0
    assert result.tool_errors == 2


# --- eviction ----------------------------------------------------------------------------------


def history_with(n: int) -> tuple:
    from claude_delegate_local.backends.base import Message

    return tuple(
        Message("user", (ToolResultBlock(tool_use_id=f"t{i}", content=f"result {i}"),))
        for i in range(n)
    )


def test_eviction_collapses_everything_past_the_most_recent_few():
    kept, dropped = loop.evict_stale_tool_results(history_with(5), keep=2)
    contents = [m.content[0].content for m in kept]
    assert contents[:3] == [loop.EVICTED_STUB] * 3
    assert contents[3:] == ["result 3", "result 4"]
    assert dropped == 3


def test_the_most_recent_results_survive_untouched():
    """The other direction: an eviction that collapsed everything would pass the count."""
    kept, _ = loop.evict_stale_tool_results(history_with(3), keep=3)
    assert [m.content[0].content for m in kept] == ["result 0", "result 1", "result 2"]


def test_eviction_keeps_the_tool_use_id():
    """Some backends validate that every tool_use has a matching result. Dropping the block
    outright would make a long delegation fail at the wire rather than merely forget."""
    kept, _ = loop.evict_stale_tool_results(history_with(3), keep=1)
    assert [m.content[0].tool_use_id for m in kept] == ["t0", "t1", "t2"]


def test_an_already_evicted_result_is_not_counted_twice():
    once, first = loop.evict_stale_tool_results(history_with(4), keep=1)
    _, second = loop.evict_stale_tool_results(once, keep=1)
    assert first == 3
    assert second == 0, "the count must be work done, not how much history is stubbed"


def test_the_loop_evicts_as_the_history_grows(registered):
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"})),
        wants(("echo", {"what": "b"})),
        wants(("echo", {"what": "c"})),
        says("done"),
    )
    result = run(backend, cfg=cfg(keep_tool_results=1))
    assert result.evicted >= 1
    stubs = [
        b.content
        for m in backend.requests[-1].messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert loop.EVICTED_STUB in stubs
    assert stubs[-1] != loop.EVICTED_STUB, "the most recent result must survive"


def test_a_generous_keep_evicts_nothing(registered):
    """The other direction, through the loop rather than the helper."""
    backend = ScriptedTurns(wants(("echo", {"what": "a"})), says("done"))
    assert run(backend, cfg=cfg(keep_tool_results=50)).evicted == 0


# --- the countdown, and the static prefix -------------------------------------------------------


def test_the_countdown_reaches_the_model_in_the_tail(registered):
    backend = ScriptedTurns(wants(("echo", {})), says("done"))
    run(backend, max_turns=5)
    tail = backend.requests[1].messages[-1].content
    assert any(isinstance(b, TextBlock) and "turns remain" in b.text for b in tail)


def test_the_last_turn_is_announced_as_the_last_one():
    assert "final turn" in loop.countdown_line(1)
    assert "final turn" not in loop.countdown_line(2)


def test_the_system_prompt_never_carries_the_turn_number(registered):
    """ADR-0011: one dynamic byte in the prefix silently disables the cluster's prefix
    cache, with no error and no symptom beyond a slower prefill. So the prompt is asserted
    byte-identical across turns, not merely 'similar'."""
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"})),
        wants(("echo", {"what": "b"})),
        says("done"),
    )
    run(backend, max_turns=9)
    prompts = {r.system for r in backend.requests}
    assert prompts == {loop.SYSTEM_PROMPT_AGENTIC}
    assert "9" not in loop.SYSTEM_PROMPT_AGENTIC


def test_the_agentic_prompt_is_not_the_one_shot_prompt():
    """The one-shot prompt tells the model it has no tools and no second turn. Sending it
    here would be a lie, and one the model would act on."""
    assert loop.SYSTEM_PROMPT_AGENTIC != loop.SYSTEM_PROMPT_ONE_SHOT
    assert "no tools" not in loop.SYSTEM_PROMPT_AGENTIC


# --- progress, ADR-0018 --------------------------------------------------------------------------


def test_progress_is_reported_once_per_turn(registered):
    seen: list[tuple[int, int]] = []

    async def record(turn, of):
        seen.append((turn, of))

    backend = ScriptedTurns(wants(("echo", {})), wants(("echo", {})), says("done"))
    run(backend, max_turns=6, report_progress=record)
    assert seen == [(1, 6), (2, 6), (3, 6)]


def test_progress_is_reported_even_for_a_single_turn(registered):
    """It exists to reset the client's idle timer, so the turn that does all the waiting is
    exactly the one that must not be skipped."""
    seen = []

    async def record(turn, of):
        seen.append((turn, of))

    run(ScriptedTurns(says("done")), max_turns=4, report_progress=record)
    assert seen == [(1, 4)]


# --- history, budget and the ledger ------------------------------------------------------


def test_reasoning_is_dropped_from_the_history_by_default(registered):
    backend = ScriptedTurns(
        CanonicalResponse(
            content=(ThinkingBlock("long thoughts"), ToolUseBlock("call-0", "echo", {})),
            finish_reason="tool_calls",
            input_tokens=1,
            output_tokens=1,
            model="served-id-1",
        ),
        says("done"),
    )
    run(backend)
    assistant = backend.requests[1].messages[1]
    assert not any(isinstance(b, ThinkingBlock) for b in assistant.content)
    assert any(isinstance(b, ToolUseBlock) for b in assistant.content)


def test_reasoning_is_kept_when_the_operator_asks_for_it(registered):
    backend = ScriptedTurns(
        CanonicalResponse(
            content=(ThinkingBlock("long thoughts"), ToolUseBlock("call-0", "echo", {})),
            finish_reason="tool_calls",
            input_tokens=1,
            output_tokens=1,
            model="served-id-1",
        ),
        says("done"),
    )
    run(backend, cfg=cfg(resend_reasoning=True))
    assistant = backend.requests[1].messages[1]
    assert any(isinstance(b, ThinkingBlock) for b in assistant.content)


def test_one_deadline_covers_the_whole_delegation(registered):
    """Not one per turn. A per-turn budget would make the real bound the timeout times
    max_turns -- for the defaults, a day and a half."""
    now = [0.0]

    def clock():
        return now[0]

    class Slow(ScriptedTurns):
        async def complete(self, request):
            now[0] += 40.0
            return await super().complete(request)

    backend = Slow(wants(("echo", {})), wants(("echo", {})), says("done"))
    with pytest.raises(loop.DispatchTimedOut):
        run(backend, cfg=cfg(dispatch_timeout=60, turn_timeout=60), clock=clock)


def test_the_ledger_counts_what_the_server_did(registered):
    """ADR-0007, extended from exit codes to the economics of the loop. The model's own
    account of how many tools it ran is not evidence; this is."""
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"}), ("refuse", {})),
        wants(("echo", {"what": "a"})),
        says("done"),
    )
    result = run(backend)
    assert result.turns == 3
    assert result.tool_calls == 3
    assert result.tool_errors == 1
    assert result.deduped == 1
    assert result.attempts == 3


# --- bash ground truth (ADR-0007) --------------------------------------------------------


def _watch_bash(*outcomes):
    """Feed `_Watch` real result blocks and read back what the ledger would report."""
    w = loop._Watch(diagnostics=False)
    for i, (bash, is_error) in enumerate(outcomes):
        w.called(
            ToolUseBlock(id=f"c{i}", name="run_bash", input={"command": "x"}),
            "error" if is_error else "ran",
            ToolResultBlock(tool_use_id=f"c{i}", content="", is_error=is_error, bash=bash),
        )
    return w


def test_the_ledger_counts_real_exits_not_the_models_account_of_them():
    w = _watch_bash(
        (BashOutcome(exit_code=0, ran=True), False),
        (BashOutcome(exit_code=3, ran=True), True),
        (BashOutcome(exit_code=0, ran=True), False),
    )
    assert (w.bash_calls, w.bash_failures, w.last_bash_exit) == (3, 1, 0)


def test_a_refused_call_counts_as_an_attempt_but_leaves_the_last_exit_alone():
    """`tool_calls` beside it counts attempts, so these must too -- a model refused ten
    times has not run zero commands. But nothing exited, so overwriting last_bash_exit with
    None would be indistinguishable from a timeout, which is a different fact."""
    w = _watch_bash(
        (BashOutcome(exit_code=7, ran=True), True),
        (BashOutcome(exit_code=None, ran=False), True),
    )
    assert (w.bash_calls, w.bash_failures, w.last_bash_exit) == (2, 2, 7)


def test_a_timeout_clears_the_last_exit_rather_than_reporting_a_number():
    w = _watch_bash(
        (BashOutcome(exit_code=0, ran=True), False),
        (BashOutcome(exit_code=None, timed_out=True, ran=True), True),
    )
    assert w.last_bash_exit is None
    assert w.bash_failures == 1


def test_other_tools_never_touch_the_bash_counters():
    w = loop._Watch(diagnostics=False)
    w.called(
        ToolUseBlock(id="r", name="read_file", input={"path": "/x"}),
        "ran",
        ToolResultBlock(tool_use_id="r", content="text"),
    )
    assert (w.bash_calls, w.bash_failures, w.last_bash_exit) == (0, 0, None)
