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
    """

    def __init__(
        self,
        cfg: Config,
        entry: ModelEntry,
        client: httpx.AsyncClient | None = None,
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
        # turn_timeout bounds the request body: a single non-streaming call *is* the turn.
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
        body.update(_effort_fields(request.effort))
        return body

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        payload = await self._post(self._entry.chat_url, self.wire_body(request), _CHAT_PATH)
        return self._from_wire(payload)

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

    async def _post(self, url: str, body: dict[str, Any], path: str) -> dict[str, Any]:
        try:
            r = await self._client.post(url, json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise BackendUnavailable(
                f"{type(e).__name__} posting to {path} on model {self._entry.key!r}."
            ) from e
        return _decode(r, path)

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

    def _from_wire(self, payload: dict[str, Any]) -> CanonicalResponse:
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
        )


# --- helpers, module level so the tests can reach them without a client -----------------


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

    Deliberately not a general parser. Histograms are skipped entirely: their `_sum` and
    `_count` are cumulative over the process, so a mean derived from them is the mean
    since boot and answers a question nobody asked. Reporting one would be worse than
    reporting nothing, because it looks like a current figure.

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
        if name in _GAUGES:
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

    hits = out.get("prefix_cache_hit_tokens")
    queries = out.get("prefix_cache_query_tokens")
    # Cumulative since the engine booted, so it is a lifetime figure and says so in its
    # name. A rate over a window would need two scrapes and a clock, which is a different
    # feature and not one `backend_status` should grow quietly.
    if isinstance(hits, int) and isinstance(queries, int) and queries > 0:
        out["prefix_cache_hit_rate_since_boot"] = round(hits / queries, 4)
    return out


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
