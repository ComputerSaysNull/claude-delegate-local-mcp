"""A stream that stopped early was handed back as a finished answer.

`_post_stream` returned `acc.payload()` whenever the frame loop ended, and the loop ends
for two very different reasons: the endpoint said it was done, or the endpoint stopped
talking. A worker killed after flushing its buffers, a proxy closing an idle stream and a
load balancer recycling a connection all look like the second, and the accumulator holds
no `finish_reason`, no `usage` and whatever text arrived before the cut. Returning that
reads as a complete reply -- `finish_reason == ""`, zero tokens -- so the caller gets half
an answer shaped exactly like a whole one, and the ledger books the turn as free.

base.py's contract says `complete()` never returns a partial; the retry decides what to do
with a turn that died mid-stream. So the cut is a `BackendUnavailable`, carrying what was
decoded, exactly as the transport-failure path already does.

Every case here drives `complete()` directly rather than through `complete_with_retry`,
so nothing retries and no test spends a stall or connect budget.
"""

import pytest

from claude_delegate_local.backends import base

from test_backends_openai_compat import backend, cfg, request
from wire_double import Clock, delta, sse, sse_response


def content(text: str) -> dict:
    """One frame carrying answer text and nothing else -- no finish reason."""
    return delta(content=text)


async def test_a_stream_that_never_said_it_finished_is_not_an_answer():
    """Two frames, then the body ends: no `finish_reason`, no usage, no `[DONE]`.

    The reply is half of what the model was saying, and the unfixed adapter returned it as
    a whole one.
    """
    clock = Clock()
    handler = lambda req: sse_response(  # noqa: E731
        sse(content("the first half"), content(" and the second"), done=False)
    )

    with pytest.raises(base.BackendUnavailable, match="without a finish reason") as caught:
        await backend(handler, config=cfg(), clock=clock).complete(request())

    partial = caught.value.partial
    assert partial is not None, "two frames were decoded and the failure reported none"
    assert partial.text == "the first half and the second"


async def test_the_cut_was_while_generating():
    """The field the retry acts on, and the reason this is not a connect failure.

    Tokens had arrived, so the request was delivered and the allowance was being spent.
    Saying otherwise sends the retry the wrong way: it would treat the turn as one that
    never reached the endpoint and spend the budget again from the start.
    """
    clock = Clock()
    handler = lambda req: sse_response(  # noqa: E731
        sse(content("decoded, then the wire went quiet"), done=False)
    )

    with pytest.raises(base.BackendUnavailable) as caught:
        await backend(handler, config=cfg(), clock=clock).complete(request())

    assert caught.value.while_generating is True


async def test_a_stream_cut_before_any_token_carries_no_partial():
    """The negative control for the first test.

    A `partial` hard-coded to an empty response would satisfy it and mean nothing. Absent
    and empty are different facts: `None` says no token ever arrived, which is the shape a
    stall reports, and an empty response says the turn produced nothing.
    """
    clock = Clock()
    handler = lambda req: sse_response(sse(done=False))  # noqa: E731

    with pytest.raises(base.BackendUnavailable) as caught:
        await backend(handler, config=cfg(), clock=clock).complete(request())

    assert caught.value.partial is None
    assert caught.value.while_generating is False


async def test_a_finish_reason_without_done_is_still_an_answer():
    """The other direction: the model said it finished, so the turn is whole.

    `[DONE]` is the protocol's own end marker and a `finish_reason` is the model's; either
    one is the endpoint saying so. Refusing this would turn a perfectly good reply into a
    retry, which on a paid prefill is worse than the bug being fixed.
    """
    clock = Clock()
    handler = lambda req: sse_response(  # noqa: E731
        sse(
            content("the whole answer"),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
            done=False,
        )
    )

    answer = await backend(handler, config=cfg(), clock=clock).complete(request())

    assert answer.text == "the whole answer"
    assert answer.finish_reason == "stop"


async def test_a_done_marker_without_a_finish_reason_is_still_an_answer():
    """The same for `[DONE]` alone, so the fix cannot be "require a finish reason".

    A reply with no content is a fact for the caller to judge (`loop.complete_with_retry`
    decides what an empty answer means); this layer only reports it.
    """
    clock = Clock()

    def handler(req):
        return sse_response(sse(content("cut off at the end"), done=True))

    answer = await backend(handler, config=cfg(), clock=clock).complete(request())

    assert answer.text == "cut off at the end"
    assert answer.finish_reason == ""
