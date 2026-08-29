"""One transient outage disabled overflow handling until the server was restarted.

Upstream cached the verdict "this backend cannot support context-overflow handling" and
never expired it, and populated it on any failure at all. So a single unreachable moment --
a route dropping, a container restarting, the endpoint being briefly busy -- turned the
feature off permanently, silently, and in a way that looked exactly like the feature working
and finding nothing wrong.

Two separate faults, and each needs its own test, because fixing either alone still leaves a
feature that switches itself off:

- a transport failure must never be cached at all, only a confirmed refusal;
- what *is* cached must expire.

The clock is injected rather than slept through. A test that waited out a fifteen-minute TTL
would be deleted by the first person whose suite it slowed down.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import server
from claude_delegate_local.backends.base import (
    BackendProtocolError,
    BackendRefused,
    BackendUnavailable,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist
WINDOW = 100_000


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "context_overflow_enabled": True,
        "overflow_probe_cache_ttl": 900.0,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(window: int = WINDOW) -> ModelEntry:
    return ModelEntry(
        key="flash", base_url=HOST, served_model_id="served-id-1", context_window=window
    )


class Endpoint:
    """A backend whose window probe can be redirected between calls, and which counts them."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0

    async def probe_window(self):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def complete(self, request):
        raise AssertionError("not used")

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def armed(check, backend, e=None):
    return asyncio.run(check.armed(backend, e or entry()))


# --- a transport failure is not evidence and must not be recorded as any ---------------------


def test_an_unreachable_endpoint_is_not_cached_and_is_retried():
    """The bug. The endpoint is down, then it is up, and the feature must come back on its
    own -- without a restart, and without waiting out any expiry, because nothing about an
    unreachable endpoint was ever worth remembering.
    """
    clock = FakeClock()
    check = server.WindowCheck(cfg(), clock=clock)
    backend = Endpoint(BackendUnavailable("route dropped"))

    ok, reason = armed(check, backend)
    assert ok is False
    assert "could not be reached" in reason

    backend.answer = WINDOW  # the endpoint comes back
    ok, reason = armed(check, backend)
    assert ok is True, "a transient outage disabled the feature until restart"
    assert reason == ""
    assert backend.calls == 2, "the second call was served from a cache it should not be in"


def test_a_confirmed_refusal_is_cached_and_not_re_probed():
    """The other direction, and the reason the cache exists at all.

    The endpoint answered. That is a real verdict about a real endpoint, so it is remembered
    -- otherwise every delegation pays a round trip to be told the same thing.
    """
    clock = FakeClock()
    check = server.WindowCheck(cfg(), clock=clock)
    backend = Endpoint(BackendRefused(404, "no such route", url_path="/v1/models"))

    assert armed(check, backend)[0] is False
    assert backend.calls == 1
    assert armed(check, backend)[0] is False
    assert backend.calls == 1, "a confirmed refusal was re-probed on every call"


def test_a_protocol_error_is_a_confirmed_refusal_too():
    """It answered; it answered with nonsense. That is still an answer about this endpoint."""
    check = server.WindowCheck(cfg(), clock=FakeClock())
    backend = Endpoint(BackendProtocolError("no data list"))
    assert armed(check, backend)[0] is False
    assert armed(check, backend)[0] is False
    assert backend.calls == 1


# --- and what is cached expires --------------------------------------------------------------


def test_the_cached_refusal_expires_and_the_endpoint_is_asked_again():
    """The second half. A cache that never expires is the bug, not the mitigation."""
    clock = FakeClock()
    ttl = 900.0
    check = server.WindowCheck(cfg(overflow_probe_cache_ttl=ttl), clock=clock)
    backend = Endpoint(BackendRefused(404, "no such route", url_path="/v1/models"))

    assert armed(check, backend)[0] is False
    assert backend.calls == 1

    clock.now += ttl - 1  # still inside the window
    assert armed(check, backend)[0] is False
    assert backend.calls == 1

    clock.now += 2  # now past it
    backend.answer = WINDOW
    assert armed(check, backend)[0] is True, "the verdict outlived its TTL"
    assert backend.calls == 2


def test_a_good_verdict_is_cached_too_so_the_probe_is_not_paid_every_call():
    check = server.WindowCheck(cfg(), clock=FakeClock())
    backend = Endpoint(WINDOW)
    assert armed(check, backend)[0] is True
    assert armed(check, backend)[0] is True
    assert backend.calls == 1


# --- the check validates, and never derives ---------------------------------------------------


def test_a_disagreement_disarms_and_names_both_numbers():
    """The operator's file stays authoritative. The feature declines rather than adopting.

    Silently taking the endpoint's number would be the auto-detection that gave upstream a
    threshold computed against a model file's architecture maximum.
    """
    check = server.WindowCheck(cfg(), clock=FakeClock())
    e = entry(window=8192)
    ok, reason = armed(check, Endpoint(1_048_576), e)
    assert ok is False
    assert "8192" in reason
    assert "1048576" in reason
    assert e.context_window == 8192, "the registry entry was mutated by a check"


def test_an_endpoint_with_nothing_to_say_about_its_window_does_not_block_the_feature():
    """The other direction. `None` is a confirmed absence, not a disagreement -- an endpoint
    that never reports a window would otherwise disable the feature for everyone on it.
    """
    check = server.WindowCheck(cfg(), clock=FakeClock())
    assert armed(check, Endpoint(None))[0] is True


def test_agreement_arms_it():
    check = server.WindowCheck(cfg(), clock=FakeClock())
    assert armed(check, Endpoint(WINDOW))[0] is True


# --- and none of it happens when the operator has not armed it -------------------------------


def test_nothing_is_probed_when_the_feature_is_off():
    """Off by default means the check costs nothing, not that it runs and returns False."""
    check = server.WindowCheck(cfg(context_overflow_enabled=False), clock=FakeClock())
    backend = Endpoint(WINDOW)
    assert armed(check, backend)[0] is False
    assert backend.calls == 0


def test_the_one_shot_path_never_pays_for_the_probe():
    """There are no turns to overflow, so a round trip to find that out is pure cost."""
    check = server.WindowCheck(cfg(), clock=FakeClock())
    backend = Endpoint(WINDOW)
    resolved, reason = asyncio.run(
        server.arm_overflow(check, backend, entry(), cfg(), agentic=False)
    )
    assert backend.calls == 0
    assert reason == ""
    assert resolved.context_overflow_enabled is True


def test_the_agentic_path_returns_a_disarmed_config_rather_than_mutating_the_servers():
    """A disarm applies to this delegation. The server's own config must be untouched, or
    one unreachable model would disable the feature for every other model in the registry.
    """
    check = server.WindowCheck(cfg(), clock=FakeClock())
    original = cfg()
    resolved, reason = asyncio.run(
        server.arm_overflow(
            check, Endpoint(BackendUnavailable("down")), entry(), original, agentic=True
        )
    )
    assert resolved.context_overflow_enabled is False
    assert original.context_overflow_enabled is True
    assert reason


def test_a_zero_ttl_is_refused_at_load():
    """There is deliberately no value meaning 'never expire'; that value is the bug."""
    with pytest.raises(Exception, match="must be positive"):
        cfg(overflow_probe_cache_ttl=0)
