"""The only adapter that ships: canonical blocks in, OpenAI chat-completions on the wire.

This is the one file that knows the wire format (ADR-0008). Everything above it speaks the
canonical, block-structured shape from `base.py`, which is why adding an Anthropic adapter
later is a new file rather than a refactor.

Flattening happens here, and only here. That is not a violation of ADR-0008 condition (a)
-- it is the point of the seam. The rule is that the *canonical* side stays
block-structured; translating it to OpenAI's flatter shape at the wire edge is this file's
whole job.

What this file does not do: retry, back off, step down on reasoning exhaustion, or decide
what an empty answer means. `Config.retry_max_attempts` and `retry_base_delay` exist, but
the response state machine that consumes them is M3. Here a failed call raises, a refused
call raises with its status and body intact, and a reply with no content comes back as a
response with no content. Every one of those is a fact the M3 machine needs; none is a
decision this layer is entitled to make.

Two things are deliberately never sent: `thinking_token_budget`, because the live server
rejects it and its documented boot flag is the wrong one (ADR-0017); and anything derived
from a model *name*, because selection is a registry lookup (ADR-0009).

Not a port. This file is new.
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
# host in a log, and from there in a paste into an issue -- and the head node is
# configuration, not a literal (see security/forbidden_strings.txt).
_CHAT_PATH = "/v1/chat/completions"
_MODELS_PATH = "/v1/models"
_METRICS_PATH = "/metrics"

# What is read out of the endpoint's Prometheus text, and nothing else. An allowlist
# rather than a filter, because the risk here is labels: `http_request_*` carry handler
# paths and `cache_config_info` carries deployment configuration, and `backend_status`
# promises never to name an endpoint. Only the metrics below are read, and of their
# labels only `reason`, which is a scheduler word.
#
# The counters are denominated in TOKENS, not requests -- measured 2026-09-05, where six
# distinct 45k calls moved `prefix_cache_queries_total` by 269,417 against 6 x 44,903.
# Reading them as request counts would understate the denominator by four orders.
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
}
# The one histogram read, and only its `_sum`/`_count` pair. Each observation is one
# request's mean seconds per output token, so `count / sum` is tokens per second since the
# engine booted. That is a lifetime mean and is named as one -- see the note in
# `read_metrics` for why that is reported here and nowhere else.
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

# Where the server puts reasoning on the way back. This stack uses "reasoning" -- measured,
# not assumed (JOURNAL 2026-08-26). "reasoning_content" is accepted second because it is
# the spelling other OpenAI-compatible servers use and costs nothing to tolerate. Either
# may be absent, and the adapter behaves the same when both are.
_REASONING_KEYS = ("reasoning", "reasoning_content")

# The values this server's own validator accepts, quoted from the 400 it returns for
# anything else (measured -- JOURNAL 2026-08-26). Exported so a test can assert that every
# level in config.EFFORT_LEVELS maps into this set: the failure mode otherwise is a 400
# discovered only after a prefill has been paid for.
SERVER_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Our vocabulary is deliberately smaller than the server's (ADR-0013), and differs in
# exactly one word: we say "off" where it says "none". Everything else is the server's own
# term, so the table has one row and the rest pass through verbatim.
_EFFORT_TRANSLATION = {"off": "none"}


class OpenAICompatBackend:
    """One endpoint, one model. Satisfies `base.Backend`.

    `client` is injectable so the tests can drive a transport double instead of a socket.
    A backend that can only be tested against a live cluster is a backend that is tested
    rarely.

    `clock` is injectable for the reason `loop.py` already gives for its own: a test of a
    duration must not spend the duration. It times the gap between the first streamed
    token and the last, which is the whole point of streaming here, and a transport double
    delivers every frame at once -- so without a fake clock the decode interval a test
    measures is zero whether the code is right or wrong, and the test cannot fail.
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
        # turn_timeout bounds the turn, but since ADR-0070 the chat call streams, and
        # httpx applies `read` per chunk rather than to the whole body -- so a stream that
        # keeps trickling would never trip it. `_accumulate` therefore enforces the bound
        # itself against `clock`, and the httpx `read` timeout now means "no chunk for this
        # long". Dropping one without adding the other would have removed the deadline.
        # dispatch_timeout spans a whole delegation and belongs to the caller above.
        #
        # connect_timeout bounds the connect phase separately, and much shorter. An earlier
        # comment here argued no such bound was needed because "a refused connection already
        # fails immediately" -- true, and irrelevant. A REFUSED connection sends RST and
        # fails in milliseconds; a DROPPED or blackholed route sends nothing at all, so
        # without a connect bound it stalled for the full turn_timeout. Measured on the
        # unfixed code: refused 0.02s, dropped still pending after 40s.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.turn_timeout, connect=cfg.connect_timeout)
        )

    # --- outbound ----------------------------------------------------------------------

    def wire_body(self, request: CanonicalRequest) -> dict[str, Any]:
        """The request as it goes on the wire. Public so a test can read it directly."""
        body: dict[str, Any] = {
            "model": self._entry.served_model_id,
            "messages": _wire_messages(request, resend_reasoning=self._cfg.resend_reasoning),
            "max_tokens": self._entry.cap_tokens(request.max_tokens),
            "temperature": request.temperature,
            "stream": True,
            # Without this the final chunk carries no `usage` and every token count in the
            # ledger, the budget and the cost record silently becomes zero. It is not an
            # optimisation: streaming without it trades the decode interval for the token
            # counts, which is a worse instrument than the one being replaced.
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
            # Only alongside `tools`, because it is a statement *about* them. Sent as the
            # wire's own word, which happens to match ours here; an adapter whose server
            # spells it differently translates, exactly as it does for effort.
            if request.tool_choice != "auto":
                body["tool_choice"] = request.tool_choice
        body.update(_effort_fields(request.effort))
        return body

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        payload, decode_seconds = await self._post_stream(
            self._entry.chat_url, self.wire_body(request), _CHAT_PATH
        )
        return self._from_wire(payload, decode_seconds=decode_seconds)

    async def _post_stream(
        self, url: str, body: dict[str, Any], path: str
    ) -> tuple[dict[str, Any], float | None]:
        """Stream the chat call and hand back one payload plus the decode interval.

        The contract above is unchanged: this returns only once the stream has ended, so
        `complete()` still never returns a partial. What streaming buys is not incremental
        delivery to the caller -- it is knowing *when the tokens arrived*, which is the
        only way to time decoding without also timing prefill.
        """
        acc = _StreamAccumulator()
        first: float | None = None
        last: float | None = None
        started = self._clock()
        try:
            async with self._client.stream(
                "POST", url, json=body, headers=self._headers
            ) as r:
                if r.status_code < 200 or r.status_code >= 300:
                    # The body has not been read yet on a streamed response, and
                    # `BackendRefused` carries it verbatim -- so read it before raising or
                    # the refusal arrives with an empty explanation.
                    await r.aread()
                    raise BackendRefused(
                        r.status_code, r.text, path, r.headers.get("Retry-After")
                    )
                async for line in r.aiter_lines():
                    if self._clock() - started > self._cfg.turn_timeout:
                        # httpx's `read` timeout is per chunk once the body streams, so a
                        # stream that trickles for ever would never trip it. This is the
                        # whole-turn bound the non-streaming call used to get for free.
                        raise BackendUnavailable(
                            f"stream from {path} on model {self._entry.key!r} ran past "
                            f"turn_timeout of {self._cfg.turn_timeout}s.",
                            while_generating=True,
                        )
                    frame = _sse_frame(line, path)
                    if frame is _SSE_DONE:
                        break
                    if frame is None:
                        continue
                    if acc.feed(frame):
                        now = self._clock()
                        if first is None:
                            first = now
                        last = now
        except httpx.HTTPError as e:
            # A read timeout is the one shape here that spent the whole allowance: the
            # request was delivered and the endpoint never answered in time. `ConnectTimeout`
            # is deliberately not included -- it is a subclass of the same
            # `TimeoutException` and spent nothing, which is the distinction the caller
            # retries on.
            #
            # Streaming makes a further distinction available -- a read timeout before the
            # first token is prefill or queueing, one mid-stream is slow decode -- and it is
            # deliberately not acted on. Splitting it would change what #167 retries, which
            # wants its own evidence rather than arriving as a side effect of this change.
            raise BackendUnavailable(
                f"{type(e).__name__} posting to {path} on model {self._entry.key!r}.",
                while_generating=isinstance(e, httpx.ReadTimeout),
            ) from e
        # `None` rather than 0.0 when one frame carried every token: the interval is
        # unknown, not instantaneous, and a zero would be divided by downstream.
        span = None if first is None or last is None or last <= first else last - first
        return acc.payload(), span

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

        vLLM names it `max_model_len` on each entry of /v1/models (JOURNAL 2026-08-29).
        The field is not in the OpenAI schema, so an endpoint that omits it is answering
        correctly and simply has nothing to say -- hence `None` rather than an error. Only
        the entry matching `served_model_id` is consulted: a host serving several models
        would otherwise have its first one speak for the one we are actually using.
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

        A 404 is an answer: this endpoint has no metrics surface, which is a fact about
        the endpoint and not a failure to ask. Only a transport failure raises, so a
        caller can tell "nothing to say" from "could not reach it" -- the distinction
        `probe_window` established and the reason neither is cached across a blip.
        """
        try:
            r = await self._client.get(self._entry.metrics_url, headers=self._headers)
        except httpx.HTTPError as e:
            raise BackendUnavailable(
                f"{type(e).__name__} getting {_METRICS_PATH} on model "
                f"{self._entry.key!r}."
            ) from e
        if r.status_code >= 400:
            # A 404 is the common case and means no metrics surface. Any other error is
            # reported the same way on purpose: this is a monitoring extra, and a caller
            # deciding whether the endpoint is healthy has already been told by `probe`.
            return None
        # An empty result and no result are one answer, not two. A 200 carrying something
        # that is not Prometheus text yields nothing parseable, and reporting `{}` for it
        # would read as "the cluster says it is doing nothing" rather than "the cluster
        # did not say".
        return read_metrics(r.text) or None

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
        self, payload: dict[str, Any], decode_seconds: float | None = None
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
        # `.get(...) or 0` would fold a real zero into "absent", and for cached_tokens
        # those are opposite answers: 0 is a measured cache miss. So these four ask
        # whether the key was there, and coerce only what was.
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
        )


# --- helpers, module level so the tests can reach them without a client -----------------

# The terminator is a sentinel rather than `None` because a blank line and the end of the
# stream are different facts, and one loop has to tell them apart.
_SSE_DONE = object()


def _sse_frame(line: str, path: str) -> Any:
    """One SSE line to a frame, to `_SSE_DONE`, or to `None` when it carries neither.

    Blank lines separate events and a line opening with a colon is a comment; both belong
    to the protocol rather than to the model. Anything else that is not a `data:` line
    means the endpoint is not speaking SSE at all, which is a protocol error and not
    something to skip quietly -- skipping it would turn a wrong endpoint into an empty
    answer, and an empty answer is a diagnosis this layer is not entitled to make.
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


class _StreamAccumulator:
    """Rebuilds one chat-completions payload from a sequence of SSE deltas.

    Reconstructs the *non-streaming* shape rather than emitting blocks as they arrive, so
    `_from_wire` stays the single place that reads the wire and `complete()` keeps its
    promise never to return a partial. Streaming is a transport change here, not a
    contract change (ADR-0070).
    """

    __slots__ = (
        "_content",
        "_fingerprint",
        "_finish",
        "_model",
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

    def feed(self, frame: dict[str, Any]) -> bool:
        """Absorb one frame. True when that frame carried generated tokens.

        The return value is what the clock times, so it has to mean *tokens* and not
        *frames*. The opening frame announcing `role` and the closing one carrying only
        `usage` are bookkeeping: counting either would put prefill back inside the
        interval this whole mechanism exists to keep it out of.
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
            return False
        choice = choices[0]
        if not isinstance(choice, dict):
            return False
        if choice.get("finish_reason") is not None:
            self._finish = str(choice["finish_reason"])
        if choice.get("stop_reason") is not None:
            self._stop = choice["stop_reason"]

        delta = choice.get("delta")
        return self._feed_delta(delta) if isinstance(delta, dict) else False

    def _feed_delta(self, delta: dict[str, Any]) -> bool:
        """The generated half of a frame. True when any of it was tokens."""
        carried = False
        for key in _REASONING_KEYS:
            piece = delta.get(key)
            if isinstance(piece, str) and piece:
                self._reasoning.append(piece)
                self._reasoning_key = key
                carried = True
                break
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            self._content.append(piece)
            carried = True
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict) and self._feed_tool_call(call):
                carried = True
        return carried

    def _feed_tool_call(self, call: dict[str, Any]) -> bool:
        """One tool-call delta. Arguments arrive split across frames and are concatenated.

        Keyed by the wire's own `index`, because a model emitting two calls interleaves
        their fragments and joining them in arrival order would splice one call's
        arguments into the other's.
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

    A *named* variable that is unset is refused here, at construction -- the same rule
    config.py and registry.py follow. Discovering it on the first delegation instead
    would waste the whole prefill.
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
    the server's own words and go out verbatim, so the top level is never remapped down and
    a caller asking for maximum effort gets it (ADR-0013).

    `chat_template_kwargs.enable_thinking` is not sent. It was the other plausible
    candidate and changed nothing measurable.

    How any of this is known, and why validating our own enum is not redundant:
    docs/ARCHITECTURE.md owns that explanation, and JOURNAL 2026-08-26 has the
    measurements. Not restated here -- one copy of a reason is the whole point.
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
            # OpenAI carries a tool result as its own message, and has nowhere to put
            # `is_error`. That flag is the server's own bookkeeping (ADR-0007) and stays
            # on the canonical side; the model sees the error text itself, which is the
            # part it can act on.
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
    Nothing here needs more than that, and a parser that tried to be complete would be a
    second, worse implementation of a format we only read four metrics out of.
    """
    inner = series.partition("{")[2].rpartition("}")[0]
    out: dict[str, str] = {}
    for part in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', inner):
        out[part[0]] = part[1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return out


def read_metrics(text: str) -> dict[str, float | int | str | None]:
    """Prometheus exposition text -> the handful of figures worth reporting.

    Deliberately not a general parser. Histograms are skipped, with one exception, and
    the reason for the rule is also the shape of the exception: their `_sum` and `_count`
    are cumulative over the process, so a mean derived from them is the mean since boot.
    Reporting one as a *current* figure would be worse than reporting nothing.

    `decode_tokens_per_second_since_boot` is read because a since-boot mean is the right
    answer to the question it is asked -- what to seed a decode-rate estimate with before
    this delegation has decoded anything (ADR-0055) -- and because being a blend over every
    concurrency regime since boot makes it conservative rather than flattering. It carries
    `_since_boot` for the same reason `prefix_cache_hit_rate_since_boot` does: the name is
    what stops it being read as current. Nothing else here may follow it without that
    question being asked again.

    Unknown names are ignored rather than collected, so a metric appearing upstream
    cannot silently widen what this returns -- `scripts/diff_endpoint_captures.py` is
    where a new name is meant to be noticed.
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


def _derive_since_boot(out: dict[str, float | int | str | None]) -> None:
    """The two figures computed from scraped ones, in place.

    Both are cumulative since the engine booted, so both say so in their names. A rate
    over a window would need two scrapes and a clock, which is a different feature and not
    one `backend_status` should grow quietly.
    """
    hits = out.get("prefix_cache_hit_tokens")
    queries = out.get("prefix_cache_query_tokens")
    if isinstance(hits, int) and isinstance(queries, int) and queries > 0:
        out["prefix_cache_hit_rate_since_boot"] = round(hits / queries, 4)

    # Inverted here rather than reported as seconds-per-token, because every caller wants
    # tokens per second and one of them multiplies it by a deadline. The intermediate keys
    # are removed: they are parser state, not a figure anyone should read.
    seconds = out.pop("_decode_seconds_sum", None)
    requests = out.pop("_decode_requests", None)
    if isinstance(seconds, float) and isinstance(requests, float) and seconds > 0:
        out["decode_tokens_per_second_since_boot"] = round(requests / seconds, 2)


def _decode(r: httpx.Response, path: str) -> dict[str, Any]:
    if r.status_code < 200 or r.status_code >= 300:
        # The header goes up verbatim. Both RFC 7231 forms are legal and either may
        # arrive; deciding what the string means -- and what to do if it means nothing --
        # is loop.py's, for the same reason finish_reason is not mapped here.
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
