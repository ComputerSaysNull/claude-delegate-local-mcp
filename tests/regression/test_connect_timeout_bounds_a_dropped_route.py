"""A dropped route must fail within connect_timeout, not turn_timeout.

The bug: the adapter built ``httpx.Timeout(cfg.turn_timeout)``, which sets connect, read,
write and pool to the same value. A *refused* connection sends RST and still failed in
milliseconds, which is what the comment defending that construction pointed at. A
*dropped* route sends nothing, so nothing bounded the connect phase short of
turn_timeout -- 1800s by default. Measured on the unfixed code: refused 0.02s, dropped
still pending after 40s.

This test opens a real socket. It must not use the project's endpoint (see
security/forbidden_strings.txt) and must not use a hostname: a hostname failure cannot be
attributed to a layer, which is the whole point of ADR-0021. 192.0.2.1 is RFC 5737
TEST-NET-1, reserved for documentation and never routed, so it drops rather than refuses.

A closed port would NOT exercise this: it sends RST and the test would pass even with the
bug present.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_delegate_local.backends import base
from claude_delegate_local.backends import openai_compat as oc
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

BLACKHOLE = "http://192.0.2.1:65535"  # RFC 5737 TEST-NET-1 -- reserved, never routed

CONNECT = 2       # what we expect to bind
TURN = 1800       # the default that used to bind instead

# The ceiling must sit BELOW the OS's own SYN-retransmission floor, or the bug hides under
# it. Windows gives up on a dropped SYN at ~21s and Linux at ~130s, so with the bug present
# the failure arrives at ~21s on Windows -- not at turn_timeout. A ceiling of 30s therefore
# PASSED against the unfixed code when this test was first written. 8s is comfortably above
# a 2s connect bound and far below both floors.
CEILING = 8


def _backend() -> oc.OpenAICompatBackend:
    cfg = Config(workspace_roots=(".",), connect_timeout=CONNECT, turn_timeout=TURN)
    entry = ModelEntry(key="blackhole", base_url=BLACKHOLE, served_model_id="served-id-1")
    return oc.OpenAICompatBackend(cfg, entry)


def _request() -> base.CanonicalRequest:
    return base.CanonicalRequest(
        system="static system prompt",
        messages=(base.Message("user", (base.TextBlock("hello"),)),),
        max_tokens=16,
        effort="low",
        temperature=0.0,
    )


@pytest.mark.integration
async def test_a_dropped_route_fails_within_connect_timeout():
    """Fails at CEILING rather than hanging.

    Without the outer wait_for, the unfixed code does not fail this test -- it blocks for
    turn_timeout, 1800s. A test that hangs on the bug it exists to catch reports nothing.
    """
    backend = _backend()
    started = time.monotonic()
    timed_out = False
    try:
        with pytest.raises(base.BackendUnavailable):
            await asyncio.wait_for(backend.complete(_request()), timeout=CEILING)
    except TimeoutError:
        timed_out = True
    finally:
        elapsed = time.monotonic() - started
        await backend.aclose()

    assert not timed_out, (
        f"still connecting after {elapsed:.1f}s against a blackholed route. "
        f"connect_timeout is {CONNECT}s, so the connect phase is being bound by "
        f"turn_timeout ({TURN}s) instead."
    )
    assert elapsed < CEILING, f"took {elapsed:.1f}s, expected close to {CONNECT}s"


def test_the_connect_bound_is_actually_installed_on_the_client():
    """The timing test above passes if the network happens to refuse rather than drop.

    This asserts the mechanism directly, so the pair cannot both pass for the wrong
    reason: connect must be bound separately, and shorter than the request bound.
    """
    timeout = _backend()._client.timeout
    assert timeout.connect == CONNECT, f"connect bound is {timeout.connect}, want {CONNECT}"
    assert timeout.read == TURN, f"read bound is {timeout.read}, want {TURN}"
    assert timeout.connect < timeout.read, "connect must be bounded shorter than the request"
