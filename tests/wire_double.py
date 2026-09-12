"""The chat wire as a transport double, now that the chat call streams (ADR-0070).

One home rather than a copy per test file: five files fake a chat reply, and the shape
they have to fake stopped being "a JSON body" on the same day. A second copy of this is
the drift that would make the next wire change cost five edits instead of one.

`/v1/models` and `/metrics` are unaffected and still answer with a whole JSON body, so
nothing here applies to them.
"""

from __future__ import annotations

import json

import httpx

from claude_delegate_local.backends import openai_compat as oc


class Clock:
    """A clock a test drives. Reading it never advances it -- the stream does.

    Needed because a transport double hands over every frame at once: without it the
    decode interval a test measures is zero whether the code is right or wrong, and a
    test of that interval cannot fail.
    """

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def sse(*frames: dict, done: bool = True) -> str:
    body = "".join("data: " + json.dumps(f) + "\n\n" for f in frames)
    return body + ("data: [DONE]\n\n" if done else "")


def delta(**kw) -> dict:
    """One chunk carrying a delta, in the wire's own shape."""
    return {"choices": [{"index": 0, "delta": kw}]}


def sse_response(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


def stream_reply(content: str = "ok", *, finish: str = "stop", usage=None,
                 model: str = "served-id-1") -> str:
    """A minimal well-formed answer, delivered as deltas."""
    return sse(
        {"model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        delta(content=content),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {"choices": [], "usage": usage or {"prompt_tokens": 7, "completion_tokens": 3}},
    )


def as_stream(body: dict) -> httpx.Response:
    """A whole-reply dict, delivered as the frames that rebuild it.

    Lets a test that is about *translation* -- reasoning blocks, tool calls, token counts,
    the four carried fields -- keep saying what it meant before the wire started
    streaming, instead of being rewritten into frames that bury the point. What it must
    not be used for is a test about the wire being malformed: the accumulator always emits
    a well-formed envelope, so a malformed body cannot survive the trip.
    """
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    head = {k: body[k] for k in ("model", "system_fingerprint") if k in body}
    frames: list[dict] = [
        {**head, "choices": [{"index": 0, "delta": {"role": message.get("role", "assistant")}}]}
    ]
    for key in oc._REASONING_KEYS:
        if isinstance(message.get(key), str) and message[key]:
            frames.append({"choices": [{"index": 0, "delta": {key: message[key]}}]})
            break
    if isinstance(message.get("content"), str) and message["content"]:
        frames.append(delta(content=message["content"]))
    for i, call in enumerate(message.get("tool_calls") or []):
        frames.append(delta(tool_calls=[{"index": i, **call}]))
    tail: dict = {"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason")}
    if "stop_reason" in choice:
        tail["stop_reason"] = choice["stop_reason"]
    frames.append({"choices": [tail]})
    frames.append({"choices": [], "usage": body.get("usage") or {}})
    return sse_response(sse(*frames))


def paced(clock: Clock, schedule):
    """A handler whose frames become readable at the times the schedule names.

    Each chunk sets the clock as it is yielded, so the adapter reads a different time for
    each frame -- which is what a real stream does and what a buffered double does not.
    """
    async def body():
        for at, chunk in schedule:
            clock.t = at
            yield chunk.encode()

    def handler(request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    return handler
