"""The only adapter that ships: canonical blocks in, OpenAI chat-completions on the wire.

This is the one file that knows the wire format (ADR-0008). Everything above it speaks the
canonical, block-structured shape from `base.py`, so an Anthropic adapter would be a new
file rather than a refactor. Flattening to OpenAI's shape happens here and only here: that
is the seam's point, not a breach of ADR-0008's rule that the *canonical* side stays
block-structured.

What this file does not do: retry, back off, step down on reasoning exhaustion, or decide
what an empty answer means -- `loop.complete_with_retry` does. Here a failed call raises, a
refused call raises with its status and body intact, and a reply with no content comes back
with no content: facts the caller decides on, not decisions this layer may make.

Never sent: `thinking_token_budget`, because the live server rejects it and its documented
boot flag is the wrong one (ADR-0017); and anything derived from a model *name*, because
selection is a registry lookup (ADR-0009).
"""

from __future__ import annotations

import json
import re
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..config import Config, ConfigError
from ..registry import ModelEntry
from .base import (
    BackendProtocolError,
    BackendRefused,
    BackendUnavailable,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalShapeError,
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

# The paths appear in error messages; the base URL never does. A host in an exception is a
# host in a log, and from there in a pasted issue -- and the head node is configuration,
# not a literal (see security/forbidden_strings.txt).
_CHAT_PATH = "/v1/chat/completions"
_MODELS_PATH = "/v1/models"
_METRICS_PATH = "/metrics"

# What is read out of the endpoint's Prometheus text, and nothing else. An allowlist, not
# a filter, because the risk is labels: `http_request_*` carry handler paths,
# `cache_config_info` carries deployment configuration, and `backend_status` promises
# never to name an endpoint. Of the labels only `reason` is read, a scheduler word.
#
# The counters are denominated in TOKENS, not requests; read as request counts they would
# understate the denominator by four orders.
_GAUGES = {
    "vllm:num_requests_running": "requests_running",
    "vllm:num_requests_waiting": "requests_waiting",
    "vllm:kv_cache_usage_perc": "kv_cache_used_fraction",
}
_COUNTERS = {
    "vllm:prefix_cache_hits_total": "prefix_cache_hit_tokens",
    "vllm:prefix_cache_queries_total": "prefix_cache_query_tokens",
    "vllm:num_preemptions_total": "preemptions",
    "vllm:external_prefix_cache_hits_total": "external_prefix_cache_hit_tokens",
    # Differenced across two scrapes by `_DecodeWindow`, the only rate that describes
    # *now*. The histogram below observes on completion, so it is blind for the whole
    # stall it should help detect: a turn producing nothing moves neither its sum nor its
    # count.
    "vllm:generation_tokens_total": "generation_tokens",
}
# The one histogram read, only its `_sum`/`_count` pair. Each observation is one request's
# mean seconds per output token, so `count / sum` is tokens per second since the engine
# booted: a lifetime mean, named as one (see `read_metrics`).
_HISTOGRAM_PAIRS = {
    "vllm:request_time_per_output_token_seconds_sum": "_decode_seconds_sum",
    "vllm:request_time_per_output_token_seconds_count": "_decode_requests",
}

# From `vllm:cache_config_info`, whose values live in its labels. Numeric configuration
# only: no path, no host, no free-form string.
_CACHE_CONFIG = {
    "kv_cache_size_tokens": "kv_cache_size_tokens",
    "num_gpu_blocks": "kv_cache_gpu_blocks",
    "enable_prefix_caching": "prefix_caching_enabled",
}

# Where the server puts reasoning on the way back. This stack uses "reasoning" (JOURNAL
# 2026-08-26); "reasoning_content", other OpenAI-compatible servers' spelling, costs
# nothing to tolerate. Either may be absent, and the adapter behaves the same when both are.
_REASONING_KEYS = ("reasoning", "reasoning_content")

# The values this server's validator accepts, quoted from the 400 it returns for anything
# else (JOURNAL 2026-08-26). Exported so a test can assert every `config.EFFORT_LEVELS`
# level maps into it: otherwise the failure is a 400 found after a prefill was paid for.
SERVER_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Our vocabulary is smaller than the server's (ADR-0013) and differs in one word: "off"
# for its "none". The rest pass through verbatim.
_EFFORT_TRANSLATION = {"off": "none"}


class OpenAICompatBackend:
    """One endpoint, one model. Satisfies `base.Backend`.

    `client` is injectable so tests drive a transport double, not a socket: a backend
    testable only against a live cluster is tested rarely.

    `clock` is injectable because it times the first streamed token to the last, and a
    transport double delivers every frame at once. With a real clock the decode interval
    a test measures is zero whether the code is right or wrong, so the test cannot fail.
    """

    def __init__(
        self,
        cfg: Config,
        entry: ModelEntry,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if entry.api_format != "openai":
            raise CanonicalShapeError(
                f"{entry.key!r} declares api_format={entry.api_format!r}; this adapter "
                "speaks 'openai'. The registry refuses unimplemented formats at load, so "
                "reaching here means the two checks have drifted apart."
            )
        self._cfg = cfg
        self._entry = entry
        self._api_key = _resolve_api_key(entry)
        self._owns_client = client is None
        self._clock = clock
        # Per backend, never shared: the window differences this endpoint's own counter,
        # and two endpoints' counters say nothing about each other. Fed `clock`, not
        # `time.monotonic`, so a test advances it as every other deadline here is tested.
        self._decode_window = _DecodeWindow()
        # `stall_timeout`, not a per-call budget. The chat call streams (ADR-0070) and
        # httpx applies `read` per chunk, so this already means "no chunk for this long",
        # the question `stall_timeout` answers. There is no whole-call bound, because
        # length is not the signal: a producing stream emits frames 0.4s apart or better,
        # so a wedged call is silent, and silence is what this catches (ADR-0100).
        # dispatch_timeout spans a whole delegation and belongs to the caller above.
        #
        # connect_timeout bounds the connect phase separately, and much shorter. A REFUSED
        # connection sends RST and fails in milliseconds; a DROPPED or blackholed route
        # sends nothing, so without a connect bound it would stall the whole read budget.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.stall_timeout, connect=cfg.connect_timeout)
        )

    # --- outbound ----------------------------------------------------------------------

    def wire_body(self, request: CanonicalRequest) -> dict[str, Any]:
        """The request as it goes on the wire. Public so a test can read it directly."""
        body: dict[str, Any] = {
            "model": self._entry.served_model_id,
            "messages": _wire_messages(request, resend_reasoning=self._cfg.resend_reasoning),
            "max_tokens": self._entry.cap_tokens(request.max_tokens),
            "temperature": request.temperature,
            "top_p": request.top_p,
            "stream": True,
            # Without this the final chunk carries no `usage`, and every token count in the
            # ledger, the budget and the cost record silently becomes zero. Not optional:
            # streaming without it would trade the token counts for the decode interval.
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
            # Only alongside `tools`, being a statement *about* them. Our word matches the
            # wire's here; an adapter whose server spells it differently translates, as
            # for effort.
            if request.tool_choice != "auto":
                body["tool_choice"] = request.tool_choice
        body.update(_effort_fields(request.effort))
        return body

    async def complete(
        self,
        request: CanonicalRequest,
        *,
        on_token: Callable[[str, str], None] | None = None,
    ) -> CanonicalResponse:
        payload, decode_seconds, prefill_seconds = await self._post_stream(
            self._entry.chat_url, self.wire_body(request), _CHAT_PATH, on_token=on_token
        )
        return self._from_wire(
            payload, decode_seconds=decode_seconds, prefill_seconds=prefill_seconds
        )

    async def _post_stream(
        self,
        url: str,
        body: dict[str, Any],
        path: str,
        *,
        on_token: Callable[[str, str], None] | None = None,
    ) -> tuple[dict[str, Any], float | None, float | None]:
        """Stream the chat call and hand back one payload plus both halves of its clock.

        Two intervals: request sent to first token (prefill), and first token to last
        (decode). Together they account for the backend call, so a run summary can say
        where the time went rather than report one span and a remainder. Knowing *when*
        tokens arrived is what streaming buys: the only way to time decoding without
        also timing prefill.

        It returns only once the stream has ended, so a successful call is whole. On a
        failing one, what the turn had decoded rides out on the exception rather than
        dying with the accumulator (ADR-0078).
        """
        acc = _StreamAccumulator()
        # When the request went out: prefill is first token minus this.
        started = self._clock()
        first: float | None = None
        last: float | None = None

        def decoded() -> CanonicalResponse | None:
            """What the stream had produced, or `None` if it never produced anything.

            Built through `_from_wire`, not handed up as a string, so a partial and a whole
            reply are one shape to every reader above -- including the reasoning-only case,
            which `answer_of` reports and a bare string would drop.
            """
            if first is None:
                return None
            span = None if last is None or last <= first else last - first
            return self._from_wire(
                acc.payload(), decode_seconds=span,
                # Known on a partial as on a whole reply: the first token has arrived, so
                # prefill is over and measured.
                prefill_seconds=max(first - started, 0.0),
            )

        try:
            async with self._client.stream(
                "POST", url, json=body, headers=self._headers
            ) as r:
                if r.status_code < 200 or r.status_code >= 300:
                    # A streamed body is unread until asked for, and `BackendRefused` carries
                    # it verbatim: read it first, or the refusal has no explanation.
                    await r.aread()
                    raise BackendRefused(
                        r.status_code, r.text, path, r.headers.get("Retry-After")
                    )
                # No whole-call bound. A trickling stream never trips the per-chunk
                # timeout, but a trickle is the endpoint working slowly, and killing it
                # guarantees nothing comes back. Length is the caller's, through the
                # stall and delegation clocks.
                async for line in r.aiter_lines():
                    frame = _sse_frame(line, path)
                    if frame is _SSE_DONE:
                        break
                    if frame is None:
                        continue
                    kind = acc.feed(frame)
                    if kind is not None:
                        now = self._clock()
                        if first is None:
                            first = now
                        last = now
                        # The predicate `decode_seconds` is measured from, so the caller
                        # hears of exactly the frames the interval counts. A role preamble
                        # or finish reason is not the decoder producing, and reporting it
                        # would tell the admission lease a running prefill had finished.
                        if on_token is not None:
                            on_token(kind, acc.piece)
        except httpx.HTTPError as e:
            # A read timeout is the one shape that spent the whole allowance: delivered,
            # and never answered in time. `ConnectTimeout` is excluded -- a subclass of the
            # same `TimeoutException` that spent nothing, the distinction the caller
            # retries on.
            #
            # Streaming says which allowance it spent: a read timeout before the first
            # token spent queueing and prefill, one mid-stream spent decode. Those are the
            # two shapes `while_generating` keeps apart (ADR-0078).
            unavailable = BackendUnavailable(
                f"{type(e).__name__} posting to {path} on model {self._entry.key!r}.",
                while_generating=isinstance(e, httpx.ReadTimeout) and first is not None,
            )
            _attach(unavailable, decoded)
            raise unavailable from e
        except BaseException as e:
            # Every other early end. `BaseException`, not `Exception`: the caller's deadline
            # cancels the task, and `CancelledError` descends from `BaseException`, so
            # `except Exception` would look right and miss the one path that matters most.
            _attach(e, decoded)
            raise
        # `None`, not 0.0, when one frame carried every token: the interval is unknown,
        # not instantaneous, and a zero would be divided by downstream.
        span = None if first is None or last is None or last <= first else last - first
        # `None` when no frame carried output, for the same reason: the prefill/decode
        # boundary was never observed, and zero would claim it was, and instantaneous.
        prefill = None if first is None else max(first - started, 0.0)
        return acc.payload(), span, prefill

    async def probe(self) -> tuple[str, ...]:
        """Model ids this endpoint reports. /v1/models is the health check (MODELS.md)."""
        payload = await self._get(self._entry.models_url, _MODELS_PATH)
        data = payload.get("data")
        if not isinstance(data, list):
            raise BackendProtocolError(
                f"{_MODELS_PATH} returned no 'data' list; got keys {sorted(payload)}."
            )
        return tuple(str(m["id"]) for m in data if isinstance(m, dict) and "id" in m)

    async def probe_window(self) -> int | None:
        """The served model's window, as this endpoint reports it. Measured, not assumed.

        vLLM names it `max_model_len` on each /v1/models entry (JOURNAL 2026-08-29). It is
        not in the OpenAI schema, so an endpoint omitting it is answering correctly, hence
        `None` rather than an error. Only the entry matching `served_model_id` counts, or a
        host serving several models would have its first speak for ours.
        """
        payload = await self._get(self._entry.models_url, _MODELS_PATH)
        data = payload.get("data")
        if not isinstance(data, list):
            raise BackendProtocolError(
                f"{_MODELS_PATH} returned no 'data' list; got keys {sorted(payload)}."
            )
        for model in data:
            if not isinstance(model, dict) or model.get("id") != self._entry.served_model_id:
                continue
            for field in ("max_model_len", "context_length", "n_ctx"):
                value = model.get(field)
                if isinstance(value, int) and value > 0:
                    return value
        return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ---------------------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def probe_cluster(self) -> dict[str, float | int | str | None] | None:
        """The serving stack's own load figures, or `None` if it publishes none.

        A 404 is an answer: this endpoint has no metrics surface. Only a transport failure
        raises, so a caller can tell "nothing to say" from "could not reach it", as with
        `probe_window`, and neither is cached across a blip.
        """
        try:
            r = await self._client.get(self._entry.metrics_url, headers=self._headers)
        except httpx.HTTPError as e:
            raise BackendUnavailable(
                f"{type(e).__name__} getting {_METRICS_PATH} on model "
                f"{self._entry.key!r}."
            ) from e
        if r.status_code >= 400:
            # A 404, the common case, means no metrics surface. Any other error is reported
            # the same way: this is a monitoring extra, and `probe` already says whether
            # the endpoint is healthy.
            return None
        # An empty result is no result. A 200 that is not Prometheus text parses to
        # nothing, and `{}` would read as "the cluster is doing nothing", not "it did not
        # say".
        scraped = read_metrics(r.text)
        if not scraped:
            return None
        generated = scraped.get("generation_tokens")
        if isinstance(generated, int):
            running = scraped.get("requests_running")
            scraped.update(
                self._decode_window.observe(
                    tokens=generated,
                    running=running if isinstance(running, (int, float)) else None,
                    now=self._clock(),
                )
            )
        return scraped

    async def _get(self, url: str, path: str) -> dict[str, Any]:
        try:
            r = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as e:
            raise BackendUnavailable(
                f"{type(e).__name__} getting {path} on model {self._entry.key!r}."
            ) from e
        return _decode(r, path)

    # --- inbound -----------------------------------------------------------------------

    def _from_wire(
        self, payload: dict[str, Any], decode_seconds: float | None = None,
        prefill_seconds: float | None = None,
    ) -> CanonicalResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise BackendProtocolError(
                f"{_CHAT_PATH} returned no choices; got keys {sorted(payload)}."
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise BackendProtocolError(f"{_CHAT_PATH} choices[0] is not an object.")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise BackendProtocolError(f"{_CHAT_PATH} choices[0] has no message object.")

        blocks: list[ContentBlock] = []

        # Reasoning first, so the canonical order matches the order it was produced in.
        for key in _REASONING_KEYS:
            reasoning = message.get(key)
            if isinstance(reasoning, str) and reasoning:
                blocks.append(ThinkingBlock(reasoning))
                break

        content = message.get("content")
        if isinstance(content, str) and content:
            blocks.append(TextBlock(content))

        for call in message.get("tool_calls") or []:
            blocks.append(_tool_use_from_wire(call))

        usage = payload.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        # `.get(...) or 0` would fold a real zero into "absent", opposite answers for
        # cached_tokens, where 0 is a measured cache miss. So these four coerce only a
        # key that was there.
        cached = details.get("cached_tokens")
        total = usage.get("total_tokens")
        stop = choice.get("stop_reason")
        fingerprint = payload.get("system_fingerprint")
        return CanonicalResponse(
            content=tuple(blocks),
            finish_reason=str(choice.get("finish_reason") or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(payload.get("model") or self._entry.served_model_id),
            cached_tokens=int(cached) if isinstance(cached, (int, float)) else None,
            total_tokens=int(total) if isinstance(total, (int, float)) else None,
            stop_reason=str(stop) if stop is not None else None,
            system_fingerprint=str(fingerprint) if fingerprint is not None else None,
            decode_seconds=decode_seconds,
            prefill_seconds=prefill_seconds,
        )


# --- helpers, module level so the tests can reach them without a client -----------------

# A sentinel, not `None`: a blank line and the end of the stream are different facts that
# one loop must tell apart.
_SSE_DONE = object()


def _sse_frame(line: str, path: str) -> Any:
    """One SSE line to a frame, to `_SSE_DONE`, or to `None` when it carries neither.

    Blank lines separate events and a line opening with a colon is a comment; both are
    protocol, not model. Any other non-`data:` line means the endpoint is not speaking
    SSE, a protocol error: skipping it would turn a wrong endpoint into an empty answer,
    a diagnosis this layer may not make.
    """
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        raise BackendProtocolError(
            f"{path} streamed a line that is not an SSE data frame: {line[:80]!r}"
        )
    data = line[len("data:") :].strip()
    if data == "[DONE]":
        return _SSE_DONE
    try:
        frame = json.loads(data)
    except ValueError as e:
        raise BackendProtocolError(
            f"{path} streamed a data frame that is not JSON: {e}"
        ) from e
    if not isinstance(frame, dict):
        raise BackendProtocolError(
            f"{path} streamed a {type(frame).__name__} frame, not a JSON object."
        )
    return frame


def _attach(error: BaseException, build: Callable[[], Any]) -> None:
    """Hang what the turn decoded on the failure, and never let that hide the failure.

    It never overwrites a partial already attached, so the innermost frame wins and a
    re-raise higher up cannot blank it. It swallows whatever `build` raises: rebuilding
    from however many frames arrived can hit a half-delivered tool call, and the partial
    is a courtesy where the exception is the fact. And it tolerates an exception that
    refuses the attribute, because the failure path is the worst place to learn one does.
    """
    try:
        if getattr(error, "partial", None) is None:
            error.partial = build()  # type: ignore[attr-defined]
    except Exception:
        pass


class _StreamAccumulator:
    """Rebuilds one chat-completions payload from a sequence of SSE deltas.

    Rebuilds the *non-streaming* shape rather than emitting blocks as they arrive, so
    `_from_wire` stays the one reader of the wire and `complete()` never returns a
    partial. Streaming is a transport change, not a contract change (ADR-0070).
    """

    __slots__ = (
        "_content",
        "_fingerprint",
        "_finish",
        "_model",
        "_piece",
        "_reasoning",
        "_reasoning_key",
        "_stop",
        "_tools",
        "_usage",
    )

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._reasoning_key = _REASONING_KEYS[0]
        self._tools: dict[int, dict[str, Any]] = {}
        self._finish: str | None = None
        self._stop: Any = None
        self._usage: dict[str, Any] | None = None
        self._model: str | None = None
        self._fingerprint: Any = None
        self._piece = ""

    @property
    def piece(self) -> str:
        """The text the last frame carried: its answer if it had one, else its reasoning."""
        return self._piece

    def feed(self, frame: dict[str, Any]) -> str | None:
        """Absorb one frame. What it carried: `"reasoning"`, `"answer"`, or None.

        The return value is what the clock times, so it means *tokens*, not *frames*, and
        names which half of the reply they are, the fact a watcher wants. The opening
        `role` frame and a closing `usage`-only one are bookkeeping: counting either would
        put prefill back inside the decode interval, and naming either would claim a
        thought or an answer that never arrived.
        """
        if self._model is None and isinstance(frame.get("model"), str):
            self._model = frame["model"]
        if self._fingerprint is None:
            self._fingerprint = frame.get("system_fingerprint")
        usage = frame.get("usage")
        if isinstance(usage, dict):
            self._usage = usage

        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        choice = choices[0]
        if not isinstance(choice, dict):
            return None
        if choice.get("finish_reason") is not None:
            self._finish = str(choice["finish_reason"])
        if choice.get("stop_reason") is not None:
            self._stop = choice["stop_reason"]

        delta = choice.get("delta")
        return self._feed_delta(delta) if isinstance(delta, dict) else None

    def _feed_delta(self, delta: dict[str, Any]) -> str | None:
        """The generated half of a frame, as its kind.

        `reasoning` when only reasoning arrived, `answer` once content or a tool-call
        fragment does -- a frame carrying both is the answer, the half a caller reads.
        `None` when it carried no generated output, so a bookkeeping frame is not counted.
        """
        kind: str | None = None
        self._piece = ""
        for key in _REASONING_KEYS:
            piece = delta.get(key)
            if isinstance(piece, str) and piece:
                self._reasoning.append(piece)
                self._reasoning_key = key
                self._piece = piece
                kind = "reasoning"
                break
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            self._content.append(piece)
            self._piece = piece
            kind = "answer"
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict) and self._feed_tool_call(call):
                kind = "answer"
        return kind

    def _feed_tool_call(self, call: dict[str, Any]) -> bool:
        """One tool-call delta. Arguments arrive split across frames and are concatenated.

        Keyed by the wire's `index`: a model emitting two calls interleaves their
        fragments, and arrival order would splice one call's arguments into the other's.
        """
        index = call.get("index")
        index = index if isinstance(index, int) else len(self._tools)
        slot = self._tools.setdefault(
            index,
            {"id": None, "type": "function", "function": {"name": None, "arguments": ""}},
        )
        if call.get("id"):
            slot["id"] = call["id"]
        if call.get("type"):
            slot["type"] = call["type"]
        fn = call.get("function")
        if not isinstance(fn, dict):
            return False
        carried = False
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
            carried = True
        args = fn.get("arguments")
        if isinstance(args, str) and args:
            slot["function"]["arguments"] += args
            carried = True
        return carried

    def payload(self) -> dict[str, Any]:
        """The frames as one non-streaming payload, for `_from_wire` to read unchanged."""
        message: dict[str, Any] = {"role": "assistant"}
        if self._reasoning:
            message[self._reasoning_key] = "".join(self._reasoning)
        message["content"] = "".join(self._content)
        if self._tools:
            message["tool_calls"] = [self._tools[i] for i in sorted(self._tools)]
        choice: dict[str, Any] = {
            "index": 0,
            "message": message,
            "finish_reason": self._finish or "",
        }
        if self._stop is not None:
            choice["stop_reason"] = self._stop
        out: dict[str, Any] = {"choices": [choice], "usage": self._usage or {}}
        if self._model is not None:
            out["model"] = self._model
        if self._fingerprint is not None:
            out["system_fingerprint"] = self._fingerprint
        return out


def _resolve_api_key(entry: ModelEntry) -> str:
    """Empty `api_key_env` means an unauthenticated endpoint, which is a normal case.

    A *named* variable that is unset is refused at construction, as config.py and
    registry.py do: found on the first delegation, it would waste the whole prefill.
    """
    if not entry.api_key_env:
        return ""
    value = os.environ.get(entry.api_key_env, "")
    if not value:
        raise ConfigError(
            f"Model {entry.key!r} sets api_key_env={entry.api_key_env!r} but that "
            "variable is unset or empty. Set it, or clear api_key_env if the endpoint "
            "needs no auth."
        )
    return value


def _effort_fields(effort: str) -> dict[str, Any]:
    """Reasoning effort, in the shape the live server accepts.

    One row of translation: our "off" is the server's "none". The other three levels are
    the server's own words and go out verbatim, so a caller asking for maximum effort gets
    it (ADR-0013). `chat_template_kwargs.enable_thinking` is not sent: it changed nothing
    measurable. Why validating our own enum is not redundant is docs/ARCHITECTURE.md's.
    """
    return {"reasoning_effort": _EFFORT_TRANSLATION.get(effort, effort)}


def _wire_messages(request: CanonicalRequest, *, resend_reasoning: bool) -> list[dict[str, Any]]:
    """Canonical messages, flattened to OpenAI's shape. Order is preserved exactly.

    Never reordered: prompt order is the caller's, and the cached prefix only pays if the
    leading tokens are bit-identical (ADR-0011).
    """
    out: list[dict[str, Any]] = []
    if request.system:
        out.append({"role": "system", "content": request.system})
    for message in request.messages:
        out.extend(_wire_message(message, resend_reasoning=resend_reasoning))
    return out


def _wire_message(message: Message, *, resend_reasoning: bool) -> list[dict[str, Any]]:
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning: list[str] = []
    results: list[dict[str, Any]] = []

    for block in message.content:
        if isinstance(block, TextBlock):
            texts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            reasoning.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, sort_keys=True),
                    },
                }
            )
        elif isinstance(block, ToolResultBlock):
            # OpenAI carries a tool result as its own message, with nowhere for
            # `is_error`. That flag is the server's bookkeeping (ADR-0007) and stays
            # canonical; the model sees the error text, the part it can act on.
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": block.content,
                }
            )

    out: list[dict[str, Any]] = []
    # Tool results answer the previous assistant turn, so they precede anything new the
    # user has to say.
    out.extend(results)

    if texts or tool_calls or (reasoning and resend_reasoning):
        payload: dict[str, Any] = {"role": message.role, "content": "".join(texts)}
        if tool_calls:
            payload["tool_calls"] = tool_calls
        if reasoning and resend_reasoning:
            payload["reasoning_content"] = "".join(reasoning)
        out.append(payload)
    return out


def _tool_use_from_wire(call: Any) -> ToolUseBlock:
    if not isinstance(call, dict):
        raise BackendProtocolError(f"{_CHAT_PATH} tool_calls contains a non-object entry.")
    fn = call.get("function")
    if not isinstance(fn, dict) or not fn.get("name"):
        raise BackendProtocolError(
            f"{_CHAT_PATH} tool call {call.get('id')!r} has no function name."
        )
    raw = fn.get("arguments")
    if raw in (None, ""):
        arguments: Any = {}
    else:
        try:
            arguments = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise BackendProtocolError(
                f"{_CHAT_PATH} tool call {call.get('id')!r} has arguments that are not "
                f"JSON: {e}. Refusing rather than passing a broken call to a tool."
            ) from e
    if not isinstance(arguments, dict):
        raise BackendProtocolError(
            f"{_CHAT_PATH} tool call {call.get('id')!r} decoded its arguments to "
            f"{type(arguments).__name__}, not an object."
        )
    return ToolUseBlock(id=str(call.get("id") or ""), name=str(fn["name"]), input=arguments)


def _labels(series: str) -> dict[str, str]:
    """The label set of one sample line, as written. No unescaping beyond the obvious.

    Prometheus text quotes label values and escapes `\\`, `\"` and `\n` inside them.
    Nothing here needs more; a complete parser would be a second, worse implementation of
    a format we read a handful of metrics out of.
    """
    inner = series.partition("{")[2].rpartition("}")[0]
    out: dict[str, str] = {}
    for part in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', inner):
        out[part[0]] = part[1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return out


def read_metrics(text: str) -> dict[str, float | int | str | None]:
    """Prometheus exposition text -> the handful of figures worth reporting.

    Not a general parser. Histograms are skipped, with one exception: their `_sum` and
    `_count` are cumulative over the process, so a mean from them is the mean since boot,
    and reporting one as a *current* figure would be worse than nothing.

    The exception, `decode_tokens_per_second_since_boot`, answers the question a
    since-boot mean fits: what to seed a decode-rate estimate with before this delegation
    has decoded anything (ADR-0055). It is *not* conservative: a lifetime per-request mean
    overprices whenever load is heavier than its average, which is when that is expensive
    (DISPATCH.md has the cost). Like `prefix_cache_hit_rate_since_boot`, its name is what
    stops it being read as current; nothing else may follow it without that question
    asked again.

    Unknown names are ignored, not collected, so a new upstream metric cannot silently
    widen what this returns -- `scripts/diff_endpoint_captures.py` is where one is noticed.
    """
    out: dict[str, float | int | str | None] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        series, _, raw = line.rpartition(" ")
        name = series.split("{", 1)[0]
        try:
            value = float(raw)
        except ValueError:
            continue
        if name in _HISTOGRAM_PAIRS:
            out[_HISTOGRAM_PAIRS[name]] = value
        elif name in _GAUGES:
            out[_GAUGES[name]] = value
        elif name in _COUNTERS:
            out[_COUNTERS[name]] = int(value)
        elif name == "vllm:num_requests_waiting_by_reason":
            reason = _labels(series).get("reason")
            if reason and reason.isidentifier():
                out[f"requests_waiting_{reason}"] = value
        elif name == "vllm:cache_config_info":
            for label, key in _CACHE_CONFIG.items():
                got = _labels(series).get(label)
                if got is None:
                    continue
                out[key] = (
                    got == "True" if key == "prefix_caching_enabled"
                    else int(got) if got.isdigit() else None
                )

    _derive_since_boot(out)
    return out


class _DecodeWindow:
    """Two scrapes and a clock, which is what a rate describing *now* needs.

    `read_metrics` stays pure, since one scrape of a cumulative counter cannot be a rate;
    this is the state that makes the difference, held per backend and never shared.

    It fills the gap the histogram leaves. `request_time_per_output_token_seconds` observes
    once per request, on completion, so a quiet turn moves nothing and the since-boot mean
    reads as it did before the stall. A counter differenced over a window falls to zero
    while the stall happens.

    Three things it refuses to report, each because the wrong answer is worse than none:

    - **The first observation.** Nothing to difference against; a rate over an assumed
      window is a guess wearing a measurement's name.
    - **A counter that went backwards.** The engine restarted, so the delta is meaningless,
      and a negative rate multiplied by a deadline would authorise a negative reply budget.
    - **Two scrapes in one clock tick.** `monotonic` has finite resolution, and a zero
      denominator is not worth a special value.

    `decode_window_seconds` ships beside the rate because the rate cannot say how old it
    is: the window is however long since someone last scraped, possibly hours. It does what
    `_since_boot` does by naming, stopping a stale figure being read as live.
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last: tuple[float, int] | None = None

    def observe(
        self, *, tokens: int, running: float | None, now: float
    ) -> dict[str, float]:
        """Record this scrape and return what the window since the last one supports."""
        previous = self._last
        self._last = (now, tokens)
        if previous is None:
            return {}

        then, before = previous
        elapsed = now - then
        if elapsed <= 0 or tokens < before:
            return {}

        generated = tokens - before
        rate = generated / elapsed
        out = {
            "decode_tokens_per_second_window": round(rate, 2),
            "decode_window_seconds": round(elapsed, 1),
            # The numerator, reported because the rate is rounded: the rate sampler must
            # tell "generated nothing" (a window inside a prefill, which it refuses) from
            # "generated a little", and should not read that off two decimal places.
            "decode_tokens_window": generated,
        }
        # Only with a divisor. `running` is sampled at the window's end, not averaged over
        # it, so this is approximate, which is why the aggregate is reported beside it.
        if running:
            out["decode_tokens_per_second_per_request_window"] = round(rate / running, 2)
        return out


def _derive_since_boot(out: dict[str, float | int | str | None]) -> None:
    """The two figures computed from scraped ones, in place.

    Both are cumulative since the engine booted, so both say so in their names. The rate
    over a window is `_DecodeWindow`'s.
    """
    hits = out.get("prefix_cache_hit_tokens")
    queries = out.get("prefix_cache_query_tokens")
    if isinstance(hits, int) and isinstance(queries, int) and queries > 0:
        out["prefix_cache_hit_rate_since_boot"] = round(hits / queries, 4)

    # Inverted here, because every caller wants tokens per second and one multiplies it by
    # a deadline. The intermediate keys are parser state, so they are removed.
    seconds = out.pop("_decode_seconds_sum", None)
    requests = out.pop("_decode_requests", None)
    if isinstance(seconds, float) and isinstance(requests, float) and seconds > 0:
        out["decode_tokens_per_second_since_boot"] = round(requests / seconds, 2)


def _decode(r: httpx.Response, path: str) -> dict[str, Any]:
    if r.status_code < 200 or r.status_code >= 300:
        # The header goes up verbatim. Either RFC 7231 form may arrive, and what it means
        # -- or what to do if it means nothing -- is loop.py's, as finish_reason is.
        raise BackendRefused(r.status_code, r.text, path, r.headers.get("Retry-After"))
    try:
        payload = r.json()
    except ValueError as e:
        raise BackendProtocolError(f"{path} returned a 2xx that is not JSON: {e}") from e
    if not isinstance(payload, dict):
        raise BackendProtocolError(
            f"{path} returned {type(payload).__name__}, not a JSON object."
        )
    return payload
