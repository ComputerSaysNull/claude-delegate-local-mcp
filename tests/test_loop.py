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
from datetime import datetime, timedelta, timezone
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
    for waited, ceiling in zip(sleep.waits, [2.0, 4.0, 8.0]):
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
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    sleep = SleepSpy()
    backend = ScriptedBackend(
        [
            BackendRefused(503, "later", "/v1/chat/completions", format_datetime(when, usegmt=True)),
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
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert loop.parse_retry_after(header, now=now) == 45.0


def test_a_retry_after_date_in_the_past_means_now_and_not_a_negative_wait():
    """A negative sleep is not a short wait; it raises inside asyncio.sleep, a long way
    from the header that caused it."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
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
