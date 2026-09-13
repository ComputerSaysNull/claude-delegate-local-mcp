"""The token-arrival seam: the adapter tells its caller when tokens land.

ADR-0070 made first-token arrival *observable* inside the adapter -- it is what
`decode_seconds` is measured from -- but nothing above the adapter could see it, because
`complete()` returns one whole response once the stream has ended. Three consumers need
the moment rather than the summary: the admission lease, which protects a prefill that is
over once decoding starts; the stall deadline, which today resets only on turn completion;
and the transcript, which can show a delegation generating rather than merely elapsed.

The callback is deliberately synchronous and argument-free. It fires on the read loop for
every frame that carried generated output, so anything it does has to be cheap -- moving a
float, or setting a flag and scheduling the real work. A consumer that needs to act once
guards its own once-ness; the adapter does not decide that for it.
"""

from __future__ import annotations

import json

import pytest
from wire_double import Clock, delta, paced

from test_backends_openai_compat import backend, request

pytestmark = pytest.mark.anyio


def _schedule(clock_times):
    """Frames carrying tokens at the named times, then a finish frame and DONE."""
    frames = [
        (at, "data: " + json.dumps(delta(content=f"tok{i}")) + "\n\n")
        for i, at in enumerate(clock_times)
    ]
    last = clock_times[-1]
    frames.append((last, "data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 9000, "completion_tokens": 30}}) + "\n\n"))
    frames.append((last, "data: [DONE]\n\n"))
    return frames


async def test_the_caller_is_told_at_every_token_arrival():
    """Every token-carrying frame, not just the first.

    First-only would serve the admission lease and nothing else. The stall deadline needs
    each arrival -- a call still producing tokens must never be killed -- so the adapter
    reports arrivals and lets the lease do its own de-duplication.
    """
    clock = Clock()
    seen: list[float] = []

    await backend(
        paced(clock, _schedule([5.0, 6.0, 7.0])), clock=clock
    ).complete(request(), on_token=lambda: seen.append(clock.t))

    assert seen == [5.0, 6.0, 7.0]


async def test_a_frame_carrying_no_tokens_is_not_an_arrival():
    """The role preamble and the finish frame are not the decoder producing anything.

    Without this the callback fires before the first token exists, which would tell the
    admission lease that a prefill still running had finished -- the exact claim the
    release is supposed to make honestly.
    """
    clock = Clock()
    seen: list[float] = []
    schedule = [
        (1.0, "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}) + "\n\n"),
        (5.0, "data: " + json.dumps(delta(content="first real token")) + "\n\n"),
        (6.0, "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 9000, "completion_tokens": 30}}) + "\n\n"),
        (6.0, "data: [DONE]\n\n"),
    ]

    await backend(paced(clock, schedule), clock=clock).complete(
        request(), on_token=lambda: seen.append(clock.t)
    )

    assert seen == [5.0]


async def test_the_callback_is_optional_and_the_response_is_unchanged():
    """`complete()` keeps its contract: one whole response, never a partial.

    The seam is added alongside the return value, not in place of it, so an adapter or a
    caller that ignores `on_token` behaves exactly as it did before.
    """
    clock = Clock()
    with_cb = await backend(
        paced(clock, _schedule([5.0, 6.0])), clock=clock
    ).complete(request(), on_token=lambda: None)

    clock2 = Clock()
    without = await backend(
        paced(clock2, _schedule([5.0, 6.0])), clock=clock2
    ).complete(request())

    assert with_cb.text == without.text
    assert with_cb.output_tokens == without.output_tokens
    assert with_cb.decode_seconds == without.decode_seconds == pytest.approx(1.0)
