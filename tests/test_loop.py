"""The one-shot delegation path.

Two things here are load-bearing beyond the obvious. Effort is resolved and sent
explicitly on every request rather than inherited from the cluster (ADR-0013), and the
system prompt is static byte for byte so the cluster's prefix cache survives (ADR-0011).
Both are invisible when broken -- a wrong effort still answers, a dynamic prompt still
answers -- so both are tested directly rather than through their symptoms.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, UTC
from email.utils import format_datetime

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import (
    BackendProtocolError,
    BackendRefused,
    BackendUnavailable,
    CanonicalResponse,
    TextBlock,
)
from claude_delegate_local.config import EFFORT_LEVELS, Config
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


def one_shot(task: str, **over):
    """A request with the boring arguments filled in, so a test line says what it means."""
    kw = {"effort": "low", "max_tokens": 100, "temperature": 1.0}
    kw.update(over)
    return loop.build_one_shot_request(delegation=loop.Delegation(task), **kw)


class SpyBackend:
    """Records the request it was given. Never opens a socket."""

    def __init__(self, response=None) -> None:
        self.requests: list = []
        self._response = response

    async def complete(self, request):
        self.requests.append(request)
        return self._response

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


# --- resolving effort -----------------------------------------------------------------


def test_an_explicit_effort_argument_beats_the_registry_row():
    assert loop.resolve_effort(cfg(), entry(default_effort="low"), "max") == "max"


def test_the_registry_row_beats_the_global_default():
    assert loop.resolve_effort(cfg(thinking_default="low"), entry(default_effort="high")) == "high"


def test_the_global_default_is_used_when_nothing_else_says():
    assert loop.resolve_effort(cfg(thinking_default="high"), entry()) == "high"


def test_effort_is_always_resolved_to_something_concrete():
    """ADR-0013: never inherited from whatever the cluster was booted with. Every path
    through resolution must name a level, so the request can carry one explicitly."""
    for explicit in (None, "", "off", "low", "high", "max"):
        assert loop.resolve_effort(cfg(), entry(), explicit) in EFFORT_LEVELS


def test_an_unlisted_effort_is_refused_before_anything_is_sent():
    """The refusal has to happen at the boundary. An unlisted level has no translation
    into the server's vocabulary, and finding that out mid-dispatch wastes the call."""
    backend = SpyBackend()

    async def go():
        await loop.run_one_shot(cfg(), entry(), backend, loop.Delegation("hello"), effort="ultra")

    with pytest.raises(loop.InvalidDelegation, match="ultra"):
        asyncio.run(go())
    assert backend.requests == [], "the backend must not have been reached at all"


def test_the_refusal_names_the_levels_that_would_have_worked():
    with pytest.raises(loop.InvalidDelegation) as caught:
        loop.resolve_effort(cfg(), entry(), "medium")
    for level in EFFORT_LEVELS:
        assert level in str(caught.value)


def test_a_valid_effort_is_not_refused():
    """The negative control: a check that refused everything would pass the test above
    and break every real call."""
    assert loop.resolve_effort(cfg(), entry(), "high") == "high"


# --- resolving the reply budget -------------------------------------------------------


def test_low_effort_uses_the_configured_budget():
    assert loop.resolve_max_tokens(cfg(max_tokens=1000), entry(), "low") == 1000


def test_high_effort_raises_the_budget_to_the_floor():
    """Reasoning is generated against the same budget as the answer, so a high effort
    on a small budget produces an empty reply with a length stop. ADR-0014."""
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000)
    assert loop.resolve_max_tokens(config, entry(), "high") == 50000
    assert loop.resolve_max_tokens(config, entry(), "max") == 50000


def test_the_floor_never_lowers_a_budget_that_is_already_higher():
    config = cfg(max_tokens=90000, thinking_max_tokens_floor=50000)
    assert loop.resolve_max_tokens(config, entry(), "high") == 90000


def test_the_model_cap_is_applied_after_the_floor():
    """The cap is the wire-facing limit: asking for more than the model accepts is a
    refusal, so it wins even over the floor that reasoning needs."""
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000)
    assert loop.resolve_max_tokens(config, entry(max_tokens_cap=8000), "high") == 8000


def test_an_explicit_budget_beats_the_configured_one():
    assert loop.resolve_max_tokens(cfg(max_tokens=1000), entry(), "low", 4096) == 4096


def test_an_explicit_budget_is_not_raised_to_the_reasoning_floor():
    """The most specific instruction there is. Multiplying it by thirty because the effort
    happens to be high would make the argument advisory, and ADR-0014's recovery already
    covers the caller who guessed too low -- at the cost of one extra dispatch."""
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000)
    assert loop.resolve_max_tokens(config, entry(), "max", 4096) == 4096


def test_the_floor_still_applies_when_no_budget_was_named():
    """The other direction, and the property ADR-0024 actually protects: an operator
    lowering DELEGATE_MAX_TOKENS must not suppress the floor. A resolver that had started
    honouring the configured value unconditionally would pass every test above."""
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000)
    assert loop.resolve_max_tokens(config, entry(), "max") == 50000


def test_the_model_cap_still_wins_over_an_explicit_budget():
    """The cap is what the wire accepts. Asking past it is a refusal, not a larger reply."""
    config = cfg(max_tokens=1000)
    assert loop.resolve_max_tokens(config, entry(max_tokens_cap=2000), "low", 999999) == 2000


def test_a_budget_below_one_is_refused_before_dispatch():
    with pytest.raises(loop.InvalidDelegation, match="at least 1"):
        loop.resolve_max_tokens(cfg(), entry(), "low", 0)


def test_an_explicit_budget_reaches_the_request():
    backend = SpyBackend(ok_response("answered"))

    async def go():
        await loop.run_one_shot(
            cfg(max_tokens=1000), entry(), backend, loop.Delegation("hi"), max_tokens=4096
        )

    asyncio.run(go())
    assert backend.requests[0].max_tokens == 4096


def test_the_step_down_stage_keeps_honouring_the_explicit_budget():
    """The stepped stage re-resolves the budget for its new level, so it is the one place
    an explicit number could quietly be dropped for the configured default."""
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000)
    backend = RecordingBackend([empty_at_length(), empty_at_length(), ok_response("late")])
    recover(config, backend, {"max_tokens_cap": 500000}, effort="max", max_tokens=4096)
    assert budgets(backend)[0] == 4096, "the first stage must send what was asked for"
    assert budgets(backend)[-1] == 4096, "the stepped stage dropped it"


# --- the static system prompt (ADR-0011) ----------------------------------------------


def test_two_requests_a_real_second_apart_have_byte_identical_system_prompts():
    """The invariant that has no symptom. A timestamp, session id or turn counter in
    the prompt still answers correctly and silently disables the cluster's prefix
    cache. Only comparing two prompts built at different wall-clock times catches it."""
    first = one_shot("x")
    time.sleep(1.1)  # crosses a wall-clock second boundary
    second = one_shot("x")
    assert first.system.encode() == second.system.encode()


def test_the_system_prompt_does_not_vary_with_the_task():
    """The task goes in the message, not the prefix. Interpolating it into the system
    prompt would give every delegation a different prefix and cache nothing."""
    a = one_shot("alpha")
    b = one_shot("beta")
    assert a.system == b.system
    assert "alpha" not in a.system


def test_the_byte_identity_check_can_actually_fail():
    """The negative test for the two above: a comparison that always holds proves
    nothing. Drift the real prompt the way a stray timestamp would and show the same
    assertion notices."""
    drifted = loop.SYSTEM_PROMPT_ONE_SHOT + time.strftime(" [%H:%M:%S]")
    assert drifted.encode() != loop.SYSTEM_PROMPT_ONE_SHOT.encode()


def test_the_system_prompt_carries_no_long_digit_run():
    """A stray epoch timestamp, pid or counter is the likely way this breaks, and it
    would not be caught by anything else until someone measured prefill."""
    import re

    assert not re.search(r"\d{4,}", loop.SYSTEM_PROMPT_ONE_SHOT)


def test_the_prompt_tells_the_model_it_has_no_file_access():
    """It cannot read files in M1, and a model that assumes otherwise answers by
    describing what it would do rather than doing it."""
    assert "no tools" in loop.SYSTEM_PROMPT_ONE_SHOT
    assert "file" in loop.SYSTEM_PROMPT_ONE_SHOT


# --- the request that goes out --------------------------------------------------------


def test_the_task_is_sent_as_the_only_user_message():
    request = one_shot("review this")
    assert len(request.messages) == 1
    assert request.messages[0].role == "user"
    assert request.messages[0].content[0].text == "review this"


def test_no_tools_are_offered_on_the_one_shot_path():
    """M1 has no tool surface for the local model. Offering one it cannot run would
    produce tool calls nothing answers."""
    request = one_shot("x")
    assert request.tools == ()


def test_an_empty_task_is_refused():
    with pytest.raises(loop.InvalidDelegation):
        one_shot("   ")


def test_a_real_task_is_not_refused():
    """The negative control for the check above."""
    assert one_shot("x").messages


def test_the_one_shot_temperature_is_the_one_that_is_sent():
    """Not tool_call_temperature: that value is low to protect tool-call syntax, and
    this path emits no tool calls, so there is no syntax to protect."""
    config = cfg(one_shot_temperature=0.7, tool_call_temperature=0.2)
    backend = SpyBackend()

    async def go():
        try:
            await loop.run_one_shot(config, entry(), backend, loop.Delegation("hello"))
        except AttributeError:
            pass  # SpyBackend returns None; the request is what matters here

    asyncio.run(go())
    assert backend.requests[0].temperature == 0.7


def test_the_resolved_effort_reaches_the_request():
    backend = SpyBackend()

    async def go():
        try:
            await loop.run_one_shot(
                cfg(), entry(default_effort="max"), backend, loop.Delegation("hello")
            )
        except AttributeError:
            pass

    asyncio.run(go())
    assert backend.requests[0].effort == "max"


# --- retry, backoff and Retry-After ----------------------------------------------------
#
# Tested at the seam rather than against a live endpoint, because every one of these is a
# decision made *about* a failure and a live endpoint will not fail on demand in four
# distinguishable ways. base.py splits the failures into four kinds precisely so this
# logic is expressible; these tests are what prove the split is being read rather than
# collapsed back into "something went wrong".


def ok_response(text: str = "hi", finish: str = "stop") -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason=finish,
        input_tokens=1,
        output_tokens=1,
        model="served-id-1",
    )


class ScriptedBackend:
    """Raises or returns whatever the script says, in order, one item per call.

    A list rather than one canned failure, because the thing under test is what happens
    across attempts. `calls` is the ground truth every test here asserts on: how many real
    dispatches happened, which is the one number a retry loop can get wrong silently.
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        item = self.script.pop(0) if self.script else ok_response()
        if isinstance(item, Exception):
            raise item
        return item

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


class SleepSpy:
    """Stands in for asyncio.sleep and records what it was asked to wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def dispatch(config, backend, *, sleep=None, jitter=None):
    """complete_with_retry with the boring arguments filled in.

    jitter defaults to the top of its range rather than something random: full jitter is
    correct in production and useless in a test, because a delay drawn from a range cannot
    be asserted against a number. Taking the maximum leaves the cap and the growth curve
    as the only things varying.
    """
    kw = {"sleep": sleep or SleepSpy(), "jitter": jitter or (lambda lo, hi: hi)}
    return asyncio.run(loop.complete_with_retry(config, backend, one_shot("hello"), **kw))


def test_an_unreachable_endpoint_is_tried_again_and_can_still_succeed():
    backend = ScriptedBackend([BackendUnavailable("route dropped"), ok_response("second try")])
    response, attempts = dispatch(cfg(retry_max_attempts=3), backend)
    assert response.text == "second try"
    assert attempts == 2
    assert backend.calls == 2


def test_the_attempt_count_is_what_really_happened_not_what_was_allowed():
    """A call that works first time reports one attempt. The count is ground truth about
    this dispatch (ADR-0007), so a caller can tell a clean success from three tries."""
    backend = ScriptedBackend([ok_response()])
    _, attempts = dispatch(cfg(retry_max_attempts=5), backend)
    assert attempts == 1
    assert backend.calls == 1


def test_exhausting_the_attempts_raises_the_last_real_failure_unchanged():
    """Not a wrapper announcing that it gave up. The four error kinds are distinguishable
    so the layer above can act on them, and hiding which one it was throws that away."""
    last = BackendUnavailable("still dropped")
    backend = ScriptedBackend([BackendUnavailable("dropped"), BackendUnavailable("again"), last])
    with pytest.raises(BackendUnavailable) as caught:
        dispatch(cfg(retry_max_attempts=3), backend)
    assert caught.value is last
    assert backend.calls == 3


def test_attempts_counts_attempts_and_not_retries():
    """retry_max_attempts=1 means send once and do not retry, not send twice."""
    backend = ScriptedBackend([BackendUnavailable("dropped"), ok_response()])
    with pytest.raises(BackendUnavailable):
        dispatch(cfg(retry_max_attempts=1), backend)
    assert backend.calls == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_temporary_refusal_is_retried(status):
    backend = ScriptedBackend([BackendRefused(status, "busy", "/v1/chat/completions")])
    response, attempts = dispatch(cfg(retry_max_attempts=2), backend)
    assert response.text == "hi"
    assert attempts == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
def test_a_refusal_describing_the_request_is_not_retried(status):
    """The negative half of the pair above, and the one that matters. base.py says a
    refusal is *usually* not retryable, so the set has to be exact -- if every status were
    retried the set would mean "any refusal" and that word would be a lie."""
    backend = ScriptedBackend([BackendRefused(status, "no", "/v1/chat/completions"), ok_response()])
    with pytest.raises(BackendRefused):
        dispatch(cfg(retry_max_attempts=5), backend)
    assert backend.calls == 1


def test_a_protocol_error_is_never_retried_even_when_a_retry_would_have_worked():
    """The backend is scripted to succeed on the second call, so a loop that wrongly
    retried this kind would pass a test that only checked the outcome. It must never reach
    that second call: a 2xx of the wrong shape means this is not the stack we meant, and
    asking again is the same mistake more slowly."""
    backend = ScriptedBackend([BackendProtocolError("choices missing"), ok_response("would work")])
    with pytest.raises(BackendProtocolError):
        dispatch(cfg(retry_max_attempts=5), backend)
    assert backend.calls == 1


def test_backoff_grows_exponentially_from_the_configured_base():
    sleep = SleepSpy()
    backend = ScriptedBackend([BackendUnavailable("x")] * 4)
    with pytest.raises(BackendUnavailable):
        dispatch(
            cfg(retry_max_attempts=4, retry_base_delay=1.0, retry_max_delay=100.0),
            backend,
            sleep=sleep,
        )
    # Four attempts, three waits: a wait exists only between attempts.
    assert sleep.waits == [1.0, 2.0, 4.0]


def test_the_cap_bounds_the_exponential_growth():
    sleep = SleepSpy()
    backend = ScriptedBackend([BackendUnavailable("x")] * 5)
    with pytest.raises(BackendUnavailable):
        dispatch(
            cfg(retry_max_attempts=5, retry_base_delay=1.0, retry_max_delay=3.0),
            backend,
            sleep=sleep,
        )
    assert sleep.waits == [1.0, 2.0, 3.0, 3.0]


def test_full_jitter_never_waits_longer_than_the_computed_delay():
    """Jitter decorrelates clients that are all guessing; it must not extend a wait. Uses
    the real random draw rather than the test stand-in, because the property being checked
    is about the range actually drawn from."""
    sleep = SleepSpy()
    backend = ScriptedBackend([BackendUnavailable("x")] * 4)
    with pytest.raises(BackendUnavailable):
        asyncio.run(
            loop.complete_with_retry(
                cfg(retry_max_attempts=4, retry_base_delay=2.0, retry_max_delay=100.0),
                backend,
                one_shot("hello"),
                sleep=sleep,
            )
        )
    assert len(sleep.waits) == 3
    for waited, ceiling in zip(sleep.waits, [2.0, 4.0, 8.0], strict=True):
        assert 0.0 <= waited <= ceiling


# --- Retry-After -----------------------------------------------------------------------


# A jitter stand-in that could never be confused with an honoured value. The tests
# below assert that Retry-After is NOT passed through it, and with the module default
# (jitter=hi) a jittered 7 is still 7 -- so the assertion would hold either way and the
# test could not fail. Caught by mutating _delay_before_retry, not by reading it.
def _loud_jitter(lo: float, hi: float) -> float:
    return 999.0


def test_retry_after_in_seconds_is_honoured_exactly_and_not_jittered():
    """The endpoint named a time. Shortening it by a random factor is not honouring it:
    jitter is for clients that are guessing, and this one was told."""
    sleep = SleepSpy()
    backend = ScriptedBackend(
        [BackendRefused(429, "slow down", "/v1/chat/completions", "7"), ok_response()]
    )
    dispatch(
        cfg(retry_max_attempts=3, retry_base_delay=1.0, retry_max_delay=60.0),
        backend,
        sleep=sleep,
        jitter=_loud_jitter,
    )
    assert sleep.waits == [7.0]


def test_retry_after_as_an_http_date_is_honoured_too():
    """Both spellings are legal (RFC 7231), and which one arrives is the server's choice.
    Honouring only the integer form is honouring the header by luck."""
    when = datetime.now(UTC) + timedelta(seconds=30)
    sleep = SleepSpy()
    backend = ScriptedBackend(
        [
            BackendRefused(
                503, "later", "/v1/chat/completions", format_datetime(when, usegmt=True)
            ),
            ok_response(),
        ]
    )
    dispatch(
        cfg(retry_max_attempts=3, retry_max_delay=600.0), backend, sleep=sleep, jitter=_loud_jitter
    )
    assert len(sleep.waits) == 1
    assert 25.0 <= sleep.waits[0] <= 31.0


def test_the_cap_bounds_a_retry_after_the_endpoint_asked_for():
    """A wait between attempts sits inside no HTTP call, so no timeout reaches it. An
    hour-long header would otherwise stall the delegation for an hour."""
    sleep = SleepSpy()
    backend = ScriptedBackend(
        [BackendRefused(429, "come back later", "/v1/chat/completions", "3600"), ok_response()]
    )
    dispatch(
        cfg(retry_max_attempts=3, retry_max_delay=20.0), backend, sleep=sleep, jitter=_loud_jitter
    )
    assert sleep.waits == [20.0]


@pytest.mark.parametrize("header", ["", "   ", "soon", "1.5", "-;", "Tue, 99 Xxx 20 99:99:99 GMT"])
def test_a_malformed_retry_after_falls_back_to_backoff_rather_than_failing(header):
    """A header is a hint from someone else's machine. Falling back is the only sane
    reading of one that cannot be parsed; failing the delegation over it is not."""
    sleep = SleepSpy()
    backend = ScriptedBackend(
        [BackendRefused(503, "?", "/v1/chat/completions", header), ok_response()]
    )
    response, _ = dispatch(
        cfg(retry_max_attempts=2, retry_base_delay=1.0, retry_max_delay=9.0),
        backend,
        sleep=sleep,
    )
    assert response.text == "hi"
    assert sleep.waits == [1.0]


def test_an_absent_retry_after_is_the_ordinary_case():
    sleep = SleepSpy()
    backend = ScriptedBackend([BackendRefused(500, "boom", "/v1/chat/completions"), ok_response()])
    dispatch(
        cfg(retry_max_attempts=2, retry_base_delay=1.5, retry_max_delay=9.0),
        backend,
        sleep=sleep,
    )
    assert sleep.waits == [1.5]


def test_parse_retry_after_reads_the_seconds_form():
    assert loop.parse_retry_after("12") == 12.0


def test_parse_retry_after_reads_the_date_form_against_a_fixed_now():
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert loop.parse_retry_after(header, now=now) == 45.0


def test_a_retry_after_date_in_the_past_means_now_and_not_a_negative_wait():
    """A negative sleep is not a short wait; it raises inside asyncio.sleep, a long way
    from the header that caused it."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
    header = format_datetime(now - timedelta(hours=2), usegmt=True)
    assert loop.parse_retry_after(header, now=now) == 0.0


def test_a_negative_seconds_retry_after_also_clamps_to_zero():
    assert loop.parse_retry_after("-5") == 0.0


@pytest.mark.parametrize("header", [None, "", "  ", "later", "1.5", "NaN"])
def test_parse_retry_after_returns_none_when_the_header_says_nothing_usable(header):
    assert loop.parse_retry_after(header) is None


# --- what the dispatch reports ---------------------------------------------------------


def test_run_one_shot_reports_the_attempts_it_took():
    backend = ScriptedBackend([BackendUnavailable("dropped"), ok_response("recovered")])
    result = asyncio.run(
        loop.run_one_shot(
            cfg(retry_max_attempts=3),
            entry(),
            backend,
            loop.Delegation("hello"),
            sleep=SleepSpy(),
        )
    )
    assert result.response.text == "recovered"
    assert result.attempts == 2
    assert result.effort in EFFORT_LEVELS


# --- empty-answer recovery (ADR-0014) --------------------------------------------------
#
# The failure being recovered from is real and measured, not hypothetical: at max effort
# with a small budget the model reasons until the budget is gone and returns null content
# with a length stop. What makes it worth testing this closely is that every mitigation
# costs a full generation at the largest budget the model allows, so both over-firing and
# under-firing are expensive -- and the two terminal states are different diagnoses that
# send the caller to different fixes.


def empty_at_length() -> CanonicalResponse:
    """The exhaustion signature: no text, stopped on length."""
    return CanonicalResponse(
        content=(), finish_reason="length", input_tokens=1, output_tokens=1, model="served-id-1"
    )


def recover(config, backend, entry_over=None, **kw):
    return asyncio.run(
        loop.run_one_shot(
            config,
            entry(**(entry_over or {})),
            backend,
            loop.Delegation("hello"),
            sleep=SleepSpy(),
            **kw,
        )
    )


def budgets(backend) -> list[int]:
    """The max_tokens of every request the backend actually received."""
    return [r.max_tokens for r in backend.seen]


def efforts(backend) -> list[str]:
    return [r.effort for r in backend.seen]


class RecordingBackend(ScriptedBackend):
    """A ScriptedBackend that also keeps the requests, so a test can assert what was sent.

    The stages are only distinguishable by the request they produce -- same task, different
    budget and level -- so a test that checks only the outcome cannot tell a floor retry
    from a step-down, or either from a loop that sent the same thing twice.
    """

    def __init__(self, script) -> None:
        super().__init__(script)
        self.seen: list = []

    async def complete(self, request):
        self.seen.append(request)
        return await super().complete(request)


def test_an_answer_that_arrives_is_not_retried_at_all():
    backend = RecordingBackend([ok_response("done")])
    result = recover(cfg(), backend)
    assert result.response.text == "done"
    assert backend.calls == 1
    assert result.reasoning_exhausted is False


def test_an_empty_answer_at_a_length_stop_is_retried_at_a_larger_budget():
    """Stage two keeps the effort level and raises the room. Keeping the level is the
    point: the level is part of the rendered prompt, so changing it would also throw away
    the prefix cache, and this stage is the one that does not have to."""
    backend = RecordingBackend([empty_at_length(), ok_response("answered on the retry")])
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="low")
    result = recover(config, backend)
    assert result.response.text == "answered on the retry"
    assert backend.calls == 2
    assert budgets(backend) == [1000, 50000]
    assert efforts(backend) == ["low", "low"]
    assert result.effort == "low"
    assert result.reasoning_exhausted is False


def test_the_retry_budget_is_the_larger_of_double_and_the_floor():
    """ADR-0014 says the larger of the two, and doubling matters when the first budget was
    already above the floor -- otherwise the "retry at a larger budget" would be a retry at
    a smaller one."""
    backend = RecordingBackend([empty_at_length(), ok_response()])
    config = cfg(max_tokens=80000, thinking_max_tokens_floor=50000, thinking_default="low")
    recover(config, backend, {"max_tokens_cap": 500000})
    assert budgets(backend) == [80000, 160000]


def test_the_model_cap_still_wins_over_the_retry_budget():
    """The cap is what the wire will accept. A retry above it is refused, which would turn
    a recoverable empty answer into a hard failure."""
    backend = RecordingBackend([empty_at_length(), ok_response()])
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="low")
    recover(config, backend, {"max_tokens_cap": 4096})
    assert budgets(backend) == [1000, 4096]


def test_a_retry_that_could_only_send_an_identical_request_is_skipped():
    """When the model's cap already pinned the first budget there is no larger budget to
    retry at, and stage two would send a byte-identical request -- a full generation bought
    for a result that cannot differ. It must step down instead of spending that call."""
    backend = RecordingBackend([empty_at_length(), ok_response("stepped")])
    config = cfg(max_tokens=100000, thinking_max_tokens_floor=50000, thinking_default="high")
    result = recover(config, backend, {"max_tokens_cap": 4096})
    assert budgets(backend) == [4096, 4096], "same budget, because the cap pins both"
    assert efforts(backend) == ["high", "low"], "so the second call must be the step-down"
    assert result.effort == "low"
    assert backend.calls == 2


def test_a_still_empty_answer_steps_the_effort_down_one_level():
    backend = RecordingBackend(
        [empty_at_length(), empty_at_length(), ok_response("answered at low")]
    )
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="high")
    result = recover(config, backend, {"max_tokens_cap": 500000})
    assert result.response.text == "answered at low"
    assert efforts(backend) == ["high", "high", "low"]
    assert result.effort == "low", "the level actually used, not the one asked for"
    assert result.reasoning_exhausted is False
    assert result.attempts == 3


def test_the_stepped_down_budget_is_recomputed_and_not_inherited():
    """A lower level no longer needs the floor the higher one forced up, so the budget is
    resolved again for it. Reusing stage two's inflated budget would keep paying for
    headroom the level does not want, on the dispatch that is already the most expensive
    because it misses the prefix cache."""
    backend = RecordingBackend([empty_at_length(), empty_at_length(), ok_response()])
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="high")
    recover(config, backend, {"max_tokens_cap": 500000})
    assert budgets(backend) == [50000, 100000, 1000]
    stepped = loop.resolve_max_tokens(config, entry(max_tokens_cap=500000), "low")
    assert budgets(backend)[2] == stepped


def test_the_step_down_table_is_derived_from_the_vocabulary():
    """Not hand-written. A second copy of EFFORT_LEVELS stops agreeing with it silently --
    by stepping to a level that no longer exists, or skipping one that does."""
    assert loop._STEP_DOWN == {"low": "off", "high": "low", "max": "high"}
    for level in EFFORT_LEVELS[1:]:
        assert loop._STEP_DOWN[level] in EFFORT_LEVELS
    assert "off" not in loop._STEP_DOWN, "there is no level below not reasoning"


def test_exhausting_every_mitigation_reports_reasoning_exhausted():
    """ADR-0014's reasoning_exhausted_budget: the word is earned only here, after a larger
    budget and a lower level have both been spent and the answer is still empty."""
    backend = RecordingBackend([empty_at_length(), empty_at_length(), empty_at_length()])
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="max")
    result = recover(config, backend, {"max_tokens_cap": 500000})
    assert result.reasoning_exhausted is True
    assert result.response.text == ""
    assert result.effort == "high", "the level it ended on"
    assert backend.calls == 3


def test_an_empty_answer_at_the_lowest_effort_is_not_called_reasoning_exhausted():
    """The wrong-diagnosis trap. With effort already off there is nothing left to disable,
    so the budget was too small for the answer -- not reasoning that would not fit. Calling
    it exhaustion sends the caller to lower an effort that is already lowest, instead of to
    shorten the task or raise the cap."""
    backend = RecordingBackend([empty_at_length(), empty_at_length()])
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="off")
    result = recover(config, backend, {"max_tokens_cap": 500000})
    assert result.response.text == ""
    assert result.reasoning_exhausted is False, "off has no level below it to step down to"
    assert efforts(backend) == ["off", "off"], "and no third call was made looking for one"
    assert backend.calls == 2


def test_an_empty_answer_that_stopped_normally_is_not_retried():
    """The over-firing trap, and the boundary ADR-0014 draws. A model that stopped on its
    own and said nothing will say nothing again; retrying buys the same non-answer twice,
    at the largest budget the model allows."""
    backend = RecordingBackend(
        [
            CanonicalResponse(
                content=(), finish_reason="stop", input_tokens=1, output_tokens=1, model="m"
            ),
            ok_response("would have answered"),
        ]
    )
    result = recover(cfg(), backend)
    assert backend.calls == 1, "no mitigation is owed to a model that simply had nothing to say"
    assert result.response.text == ""
    assert result.reasoning_exhausted is False


def test_a_truncated_answer_with_text_in_it_is_not_treated_as_exhaustion():
    """The other half of the boundary: a length stop with text is ordinary truncation. An
    answer exists and is merely cut short, and re-sending discards it."""
    backend = RecordingBackend(
        [ok_response("a partial answer that runs ou", finish="length"), ok_response("second")]
    )
    result = recover(cfg(), backend)
    assert backend.calls == 1
    assert result.response.text == "a partial answer that runs ou"
    assert result.reasoning_exhausted is False


@pytest.mark.parametrize(
    "response,expected",
    [
        (empty_at_length(), True),
        (ok_response("text", finish="length"), False),
        (
            CanonicalResponse(
                content=(), finish_reason="stop", input_tokens=0, output_tokens=0, model="m"
            ),
            False,
        ),
        (ok_response("text", finish="stop"), False),
    ],
)
def test_the_signature_needs_both_halves(response, expected):
    """Empty text AND a length stop. Either alone is a different thing entirely, and the
    parametrised negative rows are the point -- a predicate reading only one half would
    pass the positive case and fire on two cases it must not."""
    assert loop.is_empty_at_length(response) is expected


def test_transport_retries_inside_a_stage_are_counted_with_the_stages():
    """attempts is what the delegation really cost, so both loops feed the same counter. A
    count that only saw the stages would report 2 for a call that dispatched four times."""
    backend = RecordingBackend(
        [
            BackendUnavailable("dropped"),
            empty_at_length(),
            BackendUnavailable("dropped again"),
            ok_response("finally"),
        ]
    )
    config = cfg(
        max_tokens=1000,
        thinking_max_tokens_floor=50000,
        thinking_default="low",
        retry_max_attempts=3,
    )
    result = recover(config, backend, {"max_tokens_cap": 500000})
    assert result.response.text == "finally"
    assert backend.calls == 4
    assert result.attempts == 4


def test_a_hard_failure_during_recovery_still_propagates():
    """Recovery is for an empty answer, not for a broken endpoint. A refusal that is not
    worth retrying must not be swallowed by the stage machinery and reported as an empty
    result -- the caller needs to know the endpoint said no."""
    backend = RecordingBackend(
        [empty_at_length(), BackendRefused(400, "bad request", "/v1/chat/completions")]
    )
    config = cfg(max_tokens=1000, thinking_max_tokens_floor=50000, thinking_default="low")
    with pytest.raises(BackendRefused):
        recover(config, backend, {"max_tokens_cap": 500000})


def test_at_max_effort_the_budget_retry_is_skipped_and_it_steps_down_immediately():
    """Measured, 2026-08-27, and the reason the guard above earns its keep.

    At max effort against the live cluster the model never answered at any budget tried --
    512, 6144, 16384, 32768 and 65536 all came back null at a length stop, with reasoning
    growing to fill whatever it was given (48k characters at 16384 tokens, 232k at 65536).
    The same prompt at low effort answered in 7033 tokens. So raising the budget is not the
    mitigation for this failure; lowering the effort is.

    With production-shaped numbers -- the floor at or above the model cap, which is what
    high and max effort already resolve to -- there is no larger budget to retry at, so the
    guard skips a dispatch that would have spent the model's entire cap to fail again, and
    the level steps down on the second call instead. This asserts that arithmetic holds,
    because if the floor ever drops below the cap the wasted call comes back silently.
    """
    backend = RecordingBackend([empty_at_length(), ok_response("answered a level down")])
    config = cfg(max_tokens=65536, thinking_max_tokens_floor=131072, thinking_default="max")
    result = recover(config, backend, {"max_tokens_cap": 131072})
    assert budgets(backend) == [131072, 131072]
    assert efforts(backend) == ["max", "high"], "the second call steps down, it does not re-budget"
    assert backend.calls == 2
    assert result.effort == "high"


# --- the whole-delegation deadline (dispatch_timeout) ------------------------------------
#
# `dispatch_timeout` was declared, validated against turn_timeout, documented, and read by
# nothing: loop.py's own docstring called it "a gap rather than a decision". The sum of
# attempts plus the waits between them was bounded only by retry_max_attempts and
# retry_max_delay, neither of which is a time.
#
# The clock is injected for the same reason sleep is. A deadline test that spends the
# deadline is a test nobody runs twice, and every case below is driven by a fake clock the
# test advances itself -- except the one that proves the per-attempt ceiling, which needs a
# real event loop and so uses a deliberately tiny real budget.


class FakeClock:
    """A monotonic clock the test drives. Never reads the wall."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AdvancingSleep(SleepSpy):
    """A sleep that costs the clock what it was asked to wait, without waiting."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        self.clock.advance(seconds)


class SlowBackend(ScriptedBackend):
    """A backend whose every call costs the fake clock a fixed number of seconds."""

    def __init__(self, script, clock: FakeClock, seconds: float) -> None:
        super().__init__(script)
        self.clock = clock
        self.seconds = seconds

    async def complete(self, request):
        self.clock.advance(self.seconds)
        return await super().complete(request)


def test_a_delegation_that_finishes_inside_the_deadline_is_untouched():
    """The deadline must be invisible when it is not reached, or it is not a deadline."""
    clock = FakeClock()
    backend = SlowBackend([ok_response("done")], clock, seconds=5)
    response, attempts = asyncio.run(
        loop.complete_with_retry(
            cfg(dispatch_timeout=3600),
            backend,
            one_shot("hello"),
            sleep=AdvancingSleep(clock),
            jitter=lambda lo, hi: hi,
            deadline=clock() + 3600,
            clock=clock,
        )
    )
    assert response.text == "done"
    assert attempts == 1


def test_the_deadline_ends_a_delegation_whose_retries_outlive_it():
    clock = FakeClock()
    backend = SlowBackend(
        [BackendUnavailable("dropped")] * 5, clock, seconds=40
    )
    with pytest.raises(loop.DispatchTimedOut) as excinfo:
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=100, turn_timeout=100, retry_max_attempts=9),
                backend,
                one_shot("hello"),
                sleep=AdvancingSleep(clock),
                jitter=lambda lo, hi: hi,
                deadline=clock() + 100,
                clock=clock,
            )
        )
    assert backend.calls < 9, "it must stop on the clock, not on the attempt counter"
    assert excinfo.value.limit == 100


def test_a_backoff_that_would_sleep_past_the_deadline_ends_it_instead():
    """Sleeping first would burn the budget and then blame the work for the wait."""
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    backend = SlowBackend([BackendUnavailable("dropped")] * 3, clock, seconds=1)
    with pytest.raises(loop.DispatchTimedOut) as excinfo:
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=2, turn_timeout=2, connect_timeout=1, retry_max_attempts=5,
                    retry_base_delay=30.0, retry_max_delay=30.0),
                backend,
                one_shot("hello"),
                sleep=sleep,
                jitter=lambda lo, hi: hi,
                deadline=clock() + 2,
                clock=clock,
            )
        )
    assert sleep.waits == [], "the 30s backoff must not be slept at all"
    assert excinfo.value.stage == "while waiting to retry"


def test_the_refusal_names_the_setting_the_elapsed_time_and_the_stage():
    """An operator has to know it was their own deadline, and which knob it was."""
    clock = FakeClock()
    backend = SlowBackend([BackendUnavailable("dropped")] * 4, clock, seconds=60)
    with pytest.raises(loop.DispatchTimedOut) as excinfo:
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=50, turn_timeout=50, retry_max_attempts=4),
                backend,
                one_shot("hello"),
                sleep=AdvancingSleep(clock),
                jitter=lambda lo, hi: hi,
                deadline=clock() + 50,
                clock=clock,
            )
        )
    message = str(excinfo.value)
    assert "DELEGATE_DISPATCH_TIMEOUT" in message
    assert "50s" in message
    assert excinfo.value.elapsed > 0


def test_one_deadline_spans_every_stage_rather_than_restarting_per_stage():
    """Three stages each given a fresh budget would bound the delegation at three times it.

    The empty-answer recovery is three real dispatches (ADR-0014). If run_one_shot handed
    each of them its own `dispatch_timeout`, the setting would silently mean something
    other than what it says, and no test of a single stage would notice.
    """
    clock = FakeClock()
    backend = SlowBackend([empty_at_length()] * 4, clock, seconds=30)
    with pytest.raises(loop.DispatchTimedOut):
        asyncio.run(
            loop.run_one_shot(
                cfg(dispatch_timeout=50, turn_timeout=50),
                entry(),
                backend,
                loop.Delegation("hello"),
                effort="high",
                sleep=AdvancingSleep(clock),
                clock=clock,
            )
        )
    # 30s a call against a 50s shared budget: the second starts at 30s and fits, the
    # third is refused before it is sent. Had each stage been handed its own 50s, all
    # three would have run and nothing here would have noticed.
    assert backend.calls == 2


def test_a_delegation_inside_the_deadline_still_completes_through_run_one_shot():
    """The other direction: run_one_shot must not have become a timeout generator."""
    clock = FakeClock()
    backend = SlowBackend([ok_response("fine")], clock, seconds=1)
    result = asyncio.run(
        loop.run_one_shot(
            cfg(dispatch_timeout=3600),
            entry(),
            backend,
            loop.Delegation("hello"),
            sleep=AdvancingSleep(clock),
            clock=clock,
        )
    )
    assert result.response.text == "fine"


def test_a_single_attempt_is_capped_by_what_is_left_of_the_deadline():
    """turn_timeout bounds one call but knows nothing of how much delegation is left.

    Real time, deliberately: this is the one behaviour a fake clock cannot show, because
    the ceiling is enforced by the event loop. The budget is tiny so the test costs it.
    """

    class Hanging:
        calls = 0

        async def complete(self, request):
            Hanging.calls += 1
            await asyncio.sleep(30)

        async def probe(self):
            return ("served-id-1",)

        async def aclose(self):
            pass

    with pytest.raises(loop.DispatchTimedOut):
        asyncio.run(
            loop.complete_with_retry(
                cfg(dispatch_timeout=1, turn_timeout=1, connect_timeout=1),
                Hanging(),
                one_shot("hello"),
                sleep=SleepSpy(),
                jitter=lambda lo, hi: hi,
                deadline=time.monotonic() + 0.05,
            )
        )
    assert Hanging.calls == 1


# --- the one-shot keepalive -----------------------------------------------------------

class SleepingBackend(SpyBackend):
    """Takes its time, so a heartbeat beside it has something to beat through."""

    def __init__(self, response, seconds: float) -> None:
        super().__init__(response)
        self._seconds = seconds

    async def complete(self, request):
        await asyncio.sleep(self._seconds)
        return await super().complete(request)


def a_reply():
    return ok_response("ok")


def test_the_heartbeat_task_does_not_outlive_the_dispatch():
    """It is the only concurrency in the module, so the risk it introduces is a task left
    running. Counting notifications through the MCP wire cannot see this: once the request
    is finished a stray `report_progress` goes nowhere, so a leaked task beats silently.
    Asking asyncio directly is what actually catches it.
    """
    beats: list[float] = []

    async def on_alive(elapsed, of):
        beats.append(elapsed)

    async def go():
        await loop.run_one_shot(
            cfg(keepalive_interval=1), entry(), SleepingBackend(a_reply(), 2.5),
            loop.Delegation("hello"), on_alive=on_alive,
        )
        during = len(beats)
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.sleep(2.5)
        return during, len(beats), others

    during, after, others = asyncio.run(go())
    assert during >= 2, f"the heartbeat did not beat during a 2.5s dispatch: {beats}"
    assert not others, f"a task outlived the dispatch: {others}"
    assert after == during, f"it went on beating after the dispatch ended: {beats}"


def test_a_heartbeat_that_raises_stops_beating_and_nothing_else():
    """It exists to protect a long delegation from being abandoned. One that instead
    killed the delegation -- because a notification could not be delivered, which is not
    even evidence the client is gone -- would be strictly worse than not having it.

    Driven through `run_one_shot` rather than through a client, because a progress handler
    that raises on the *client* side never reaches the server's callback at all, and a test
    written that way passes whatever the server does with the failure.
    """
    calls: list[int] = []

    async def on_alive(elapsed, of):
        calls.append(1)
        raise RuntimeError("nowhere to send it")

    async def go():
        result = await loop.run_one_shot(
            cfg(keepalive_interval=1), entry(), SleepingBackend(a_reply(), 2.5),
            loop.Delegation("hello"), on_alive=on_alive,
        )
        return result.response.text

    assert asyncio.run(go()) == "ok", "a failing heartbeat took the delegation with it"
    assert len(calls) == 1, (
        f"it kept calling a callback that had already failed: {len(calls)} times")


class CountingBackend(SpyBackend):
    """Counts the tasks alive around it, from inside the await they run beside."""

    def __init__(self, response) -> None:
        super().__init__(response)
        self.others = -1

    async def complete(self, request):
        await asyncio.sleep(0.05)
        me = asyncio.current_task()
        self.others = len([t for t in asyncio.all_tasks() if t is not me])
        return await super().complete(request)


def test_without_a_callback_no_heartbeat_task_is_started():
    """Every other caller of `run_one_shot` passes nothing, so the common path must not
    pay for a task nobody reads. Counted against the same dispatch with a callback rather
    than against a fixed number, so the assertion measures the heartbeat and not whatever
    else the runtime happens to have running.
    """
    async def go(on_alive):
        backend = CountingBackend(a_reply())
        await loop.run_one_shot(
            cfg(keepalive_interval=30), entry(), backend,
            loop.Delegation("hello"), on_alive=on_alive,
        )
        return backend.others

    async def beat(elapsed, of):
        pass

    without = asyncio.run(go(None))
    with_one = asyncio.run(go(beat))
    assert with_one == without + 1, (
        f"a callback added {with_one - without} task(s), not one -- so this cannot tell "
        "whether the no-callback path starts one")
