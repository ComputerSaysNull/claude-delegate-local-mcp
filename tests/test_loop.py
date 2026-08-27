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

import pytest

from claude_delegate_local import loop
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
