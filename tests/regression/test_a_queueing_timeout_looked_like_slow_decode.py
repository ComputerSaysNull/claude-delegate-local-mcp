"""A read timeout that never decoded a token reported itself as slow decode.

`_post_stream` set `while_generating=isinstance(e, httpx.ReadTimeout)` -- true for *any*
read timeout, whether or not a token had arrived. `first` was a local in the same function
and was not consulted, so a timeout spent entirely on queueing and prefill arrived at
`_retry_is_plausible` claiming to have spent a generating allowance, and the retry it
refuses is exactly the one most likely to sail through.

ADR-0078 fixed the identical literal on the sibling path -- the adapter's whole-turn bound
-- and called it "a literal where a predicate belonged". This is that predicate, thirty
lines away, on the path #167 introduced the field for.

Both directions are asserted. A fix that hard-codes `False` would satisfy the first test
and break the field's actual purpose, so the second is the control that catches it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from claude_delegate_local.backends import base

from test_backends_openai_compat import backend, cfg, request
from wire_double import Clock


def frame(content: str) -> str:
    body = {"choices": [{"index": 0, "delta": {"content": content}}]}
    return "data: " + json.dumps(body) + "\n\n"


def timing_out_at_once():
    """A read timeout with no byte of the body ever delivered.

    Raised from the handler rather than from inside the body generator, because that is
    where a real prefill timeout fires: the request was accepted and the endpoint had not
    begun answering, so there is nothing for `aiter_lines` to have iterated.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out before the first token", request=req)

    return handler


def timing_out_after(clock: Clock, content: str):
    """A stream that decodes one frame and then stops answering.

    Lazy on purpose. A buffered `httpx.Response` hands the adapter the whole body before
    anything can fail, so the mid-stream case would pass with the distinction deleted.
    """

    async def body():
        clock.t = 1.0
        yield frame(content).encode()
        clock.t = 2.0
        raise httpx.ReadTimeout("timed out mid-stream")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    return handler


async def test_a_read_timeout_before_the_first_token_was_not_generating():
    """The bug. Nothing decoded, so no generating allowance was spent."""
    clock = Clock()
    with pytest.raises(base.BackendUnavailable) as caught:
        await backend(
            timing_out_at_once(), config=cfg(turn_timeout=60), clock=clock
        ).complete(request())

    assert caught.value.while_generating is False


async def test_a_read_timeout_after_a_token_is_still_generating():
    """The control, and the half that must not regress.

    A stream that produced a token and then stalled really was decoding, and a decoder
    that is slow is likely to be slow again -- which is the case `while_generating` exists
    to make the retry pessimistic about.
    """
    clock = Clock()
    with pytest.raises(base.BackendUnavailable) as caught:
        await backend(
            timing_out_after(clock, "decoded this much"),
            config=cfg(turn_timeout=60),
            clock=clock,
        ).complete(request())

    assert caught.value.while_generating is True
