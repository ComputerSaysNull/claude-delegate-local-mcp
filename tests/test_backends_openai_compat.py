"""The OpenAI adapter, driven against a transport double rather than a live cluster.

Every failure mode has its own test, because the caller acts differently on each and M3
will build retry on that distinction. Each negative test asserts the check *fires*.
"""

from __future__ import annotations

import json

import httpx
import pytest

from claude_delegate_local.backends import base
from claude_delegate_local.backends import openai_compat as oc
from claude_delegate_local.config import EFFORT_LEVELS, Config, ConfigError
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


def entry(**over) -> ModelEntry:
    kw = {
        "key": "flash",
        "base_url": HOST,
        "served_model_id": "served-id-1",
    }
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def backend(handler=None, *, config=None, model=None) -> oc.OpenAICompatBackend:
    """An adapter wired to a transport double. No socket is ever opened."""
    handler = handler or (lambda request: httpx.Response(200, json=reply()))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return oc.OpenAICompatBackend(config or cfg(), model or entry(), client=client)


def reply(**over) -> dict:
    """A minimal well-formed chat completion."""
    body = {
        "model": "served-id-1",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    body.update(over)
    return body


def request(**over) -> base.CanonicalRequest:
    kw = {
        "system": "static system prompt",
        "messages": (base.Message("user", (base.TextBlock("hello"),)),),
        "max_tokens": 1000,
        "effort": "low",
        "temperature": 0.0,
    }
    kw.update(over)
    return base.CanonicalRequest(**kw)  # type: ignore[arg-type]


def capture():
    """A handler that records the request it was given and answers successfully."""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=reply())

    return handler, seen


# --- the outgoing request --------------------------------------------------------------


async def test_posts_to_the_chat_path_with_the_served_model_id():
    handler, seen = capture()
    await backend(handler).complete(request())
    assert seen["url"] == f"{HOST}/v1/chat/completions"
    assert seen["body"]["model"] == "served-id-1"


async def test_never_sends_thinking_token_budget():
    """ADR-0017: the live server rejects it and its documented boot flag is the wrong one."""
    handler, seen = capture()
    await backend(handler).complete(request(effort="max"))
    assert "thinking_token_budget" not in json.dumps(seen["body"])


async def test_max_tokens_is_clamped_by_the_per_model_cap():
    handler, seen = capture()
    await backend(handler, model=entry(max_tokens_cap=256)).complete(request(max_tokens=99999))
    assert seen["body"]["max_tokens"] == 256


async def test_max_tokens_is_untouched_when_the_cap_is_zero():
    handler, seen = capture()
    await backend(handler, model=entry(max_tokens_cap=0)).complete(request(max_tokens=4321))
    assert seen["body"]["max_tokens"] == 4321


async def test_system_prompt_leads_and_order_is_preserved():
    """ADR-0011: the cached prefix only pays if the leading tokens are bit-identical."""
    handler, seen = capture()
    req = request(
        messages=(
            base.Message("user", (base.TextBlock("first"),)),
            base.Message("assistant", (base.TextBlock("second"),)),
            base.Message("user", (base.TextBlock("third"),)),
        )
    )
    await backend(handler).complete(req)
    assert [m["content"] for m in seen["body"]["messages"]] == [
        "static system prompt",
        "first",
        "second",
        "third",
    ]


async def test_tools_are_declared_in_the_function_shape():
    handler, seen = capture()
    spec = base.ToolSpec("read_file", "Read a file.", {"type": "object", "properties": {}})
    await backend(handler).complete(request(tools=(spec,)))
    assert seen["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


async def test_no_tools_key_when_none_are_declared():
    handler, seen = capture()
    await backend(handler).complete(request())
    assert "tools" not in seen["body"]


# --- auth ------------------------------------------------------------------------------


async def test_no_authorization_header_when_the_endpoint_needs_no_auth():
    """An empty api_key_env is a normal configuration, not an error."""
    handler, seen = capture()
    await backend(handler, model=entry(api_key_env="")).complete(request())
    assert "authorization" not in {k.lower() for k in seen["headers"]}


async def test_authorization_header_when_the_named_variable_is_set(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN_VAR", "s3cret")
    handler, seen = capture()
    await backend(handler, model=entry(api_key_env="SOME_TOKEN_VAR")).complete(request())
    assert seen["headers"]["authorization"] == "Bearer s3cret"


def test_construction_refuses_a_named_but_unset_api_key(monkeypatch):
    """Refused at construction, never at first use -- the rule config.py already follows."""
    monkeypatch.delenv("SOME_TOKEN_VAR", raising=False)
    with pytest.raises(ConfigError, match="unset or empty"):
        oc.OpenAICompatBackend(cfg(), entry(api_key_env="SOME_TOKEN_VAR"))


def test_adapter_refuses_a_format_it_does_not_speak():
    """The registry refuses this at load too. Two sites, checked independently."""
    with pytest.raises(base.CanonicalShapeError, match="drifted apart"):
        oc.OpenAICompatBackend(cfg(), entry(api_format="anthropic"))


# --- transport and refusal -------------------------------------------------------------


async def test_non_2xx_becomes_refused_carrying_status_and_body():
    def handler(req):
        return httpx.Response(400, text="thinking_token_budget needs a different flag")

    with pytest.raises(base.BackendRefused) as caught:
        await backend(handler).complete(request())
    assert caught.value.status == 400
    assert "different flag" in caught.value.body


async def test_connect_failure_becomes_unavailable():
    def handler(req):
        raise httpx.ConnectError("no route", request=req)

    with pytest.raises(base.BackendUnavailable, match="ConnectError"):
        await backend(handler).complete(request())


async def test_timeout_becomes_unavailable():
    def handler(req):
        raise httpx.ReadTimeout("too slow", request=req)

    with pytest.raises(base.BackendUnavailable, match="ReadTimeout"):
        await backend(handler).complete(request())


async def test_no_error_message_leaks_the_endpoint():
    """The head node is configuration, never a literal -- including inside an exception.

    An exception string reaches a log, and from there a pasted issue comment. The paths
    are named; the host is not.
    """
    cases = [
        lambda req: httpx.Response(500, text="boom"),
        lambda req: (_ for _ in ()).throw(httpx.ConnectError("no route", request=req)),
        lambda req: httpx.Response(200, text="not json"),
        lambda req: httpx.Response(200, json={"no": "choices"}),
    ]
    for handler in cases:
        with pytest.raises(base.BackendError) as caught:
            await backend(handler).complete(request())
        assert "example.com" not in str(caught.value)
        assert "8000" not in str(caught.value)


# --- malformed 2xx ---------------------------------------------------------------------


async def test_a_2xx_that_is_not_json_is_a_protocol_error():
    with pytest.raises(base.BackendProtocolError, match="not JSON"):
        await backend(lambda req: httpx.Response(200, text="<html>oops</html>")).complete(
            request()
        )


async def test_a_2xx_json_array_is_a_protocol_error():
    with pytest.raises(base.BackendProtocolError, match="not a JSON object"):
        await backend(lambda req: httpx.Response(200, json=[1, 2])).complete(request())


async def test_missing_choices_is_a_protocol_error():
    with pytest.raises(base.BackendProtocolError, match="no choices"):
        await backend(lambda req: httpx.Response(200, json={"usage": {}})).complete(request())


async def test_missing_message_is_a_protocol_error():
    body = {"choices": [{"finish_reason": "stop"}]}
    with pytest.raises(base.BackendProtocolError, match="no message object"):
        await backend(lambda req: httpx.Response(200, json=body)).complete(request())


async def test_tool_call_arguments_that_are_not_json_are_a_protocol_error():
    body = reply(
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read_file", "arguments": "{oops"}}
                    ],
                },
            }
        ]
    )
    with pytest.raises(base.BackendProtocolError, match=r"not.*JSON"):
        await backend(lambda req: httpx.Response(200, json=body)).complete(request())


async def test_tool_call_arguments_that_decode_to_a_scalar_are_a_protocol_error():
    body = reply(
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read_file", "arguments": "42"}}
                    ],
                },
            }
        ]
    )
    with pytest.raises(base.BackendProtocolError, match="not an object"):
        await backend(lambda req: httpx.Response(200, json=body)).complete(request())


async def test_tool_call_without_a_function_name_is_a_protocol_error():
    body = reply(
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "tool_calls": [{"id": "c1", "function": {}}]},
            }
        ]
    )
    with pytest.raises(base.BackendProtocolError, match="no function name"):
        await backend(lambda req: httpx.Response(200, json=body)).complete(request())


# --- the incoming response -------------------------------------------------------------


async def test_text_and_usage_come_back_canonical():
    r = await backend().complete(request())
    assert r.text == "ok"
    assert (r.input_tokens, r.output_tokens) == (7, 3)
    assert r.finish_reason == "stop"
    assert r.model == "served-id-1"


async def test_the_four_fields_the_endpoint_reports_are_carried():
    """What the cluster says about its own work, rather than what we guess about it.

    `cached_tokens` is the one that matters: nothing about prefix reuse is observable
    without it, and it was discarded for a fortnight while the batch tools were argued
    over on the strength of it (ADR-0051).
    """
    body = reply(
        system_fingerprint="engine-build-and-config-hash",
        choices=[{"finish_reason": "stop", "stop_reason": "eos",
                  "message": {"role": "assistant", "content": "ok"}}],
        usage={"prompt_tokens": 44905, "completion_tokens": 16, "total_tokens": 44921,
               "prompt_tokens_details": {"cached_tokens": 44800}},
    )
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert r.cached_tokens == 44800
    assert r.total_tokens == 44921
    assert r.stop_reason == "eos"
    assert r.system_fingerprint == "engine-build-and-config-hash"


async def test_a_cache_miss_is_zero_and_an_endpoint_that_cannot_say_is_none():
    """Opposite answers, and folding them together is the failure this field exists to
    end. `0` is a measured miss on an endpoint that reports caching; `None` is an
    endpoint that reports nothing, where a summed zero would silently claim every call
    missed. `or 0` in the adapter would produce the same value for both.
    """
    missed = reply(usage={"prompt_tokens": 7, "completion_tokens": 3,
                          "prompt_tokens_details": {"cached_tokens": 0}})
    r = await backend(lambda req: httpx.Response(200, json=missed)).complete(request())
    assert r.cached_tokens == 0, "a measured miss must not read as absent"

    silent = reply()  # no prompt_tokens_details at all
    r = await backend(lambda req: httpx.Response(200, json=silent)).complete(request())
    assert r.cached_tokens is None, "an endpoint that says nothing must not read as a miss"
    assert r.total_tokens is None
    assert r.stop_reason is None
    assert r.system_fingerprint is None


async def test_a_null_stop_reason_is_absent_not_the_empty_string():
    """vLLM sends `stop_reason: null` on a normal stop, which is what this endpoint does
    on every call measured on 2026-09-05. It must not become `""`, which `finish_reason`
    uses for a different thing -- a wire value that was genuinely empty."""
    body = reply(choices=[{"finish_reason": "stop", "stop_reason": None,
                           "message": {"role": "assistant", "content": "ok"}}])
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert r.stop_reason is None
    assert r.finish_reason == "stop"


async def test_null_content_at_length_is_a_response_not_an_error():
    """ADR-0014 reproduced through the adapter. M3 decides what it means; we report it.

    If a later change makes this raise, the state machine loses the one signal that
    tells reasoning exhaustion apart from a genuine refusal.
    """
    body = reply(
        choices=[{"finish_reason": "length", "message": {"role": "assistant", "content": None}}],
        usage={"prompt_tokens": 11, "completion_tokens": 2048},
    )
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert r.content == ()
    assert r.text == ""
    assert r.finish_reason == "length"
    assert r.output_tokens == 2048


async def test_reasoning_content_becomes_its_own_block():
    body = reply(
        choices=[
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "answer", "reasoning_content": "why"},
            }
        ]
    )
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert r.thinking == "why"
    assert r.text == "answer"
    assert isinstance(r.content[0], base.ThinkingBlock)


async def test_a_response_without_reasoning_is_handled_identically():
    r = await backend().complete(request())
    assert r.thinking == ""


async def test_tool_calls_become_tool_use_blocks():
    body = reply(
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_7",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "a.py"}',
                            },
                        }
                    ],
                },
            }
        ]
    )
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert len(r.tool_uses) == 1
    call = r.tool_uses[0]
    assert (call.id, call.name, call.input) == ("call_7", "read_file", {"path": "a.py"})


async def test_empty_tool_arguments_decode_to_an_empty_object():
    body = reply(
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"id": "c", "function": {"name": "ping", "arguments": ""}}],
                },
            }
        ]
    )
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert r.tool_uses[0].input == {}


# --- the round trip -------------------------------------------------------------------


async def test_a_tool_exchange_survives_the_round_trip():
    """Block structure in, block structure out, with OpenAI's flatter shape in between."""
    handler, seen = capture()
    req = request(
        messages=(
            base.Message("user", (base.TextBlock("read it"),)),
            base.Message(
                "assistant", (base.ToolUseBlock("call_1", "read_file", {"path": "a.py"}),)
            ),
            base.Message("user", (base.ToolResultBlock("call_1", "file contents"),)),
        )
    )
    await backend(handler).complete(req)
    wire = seen["body"]["messages"]

    assistant = next(m for m in wire if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}

    tool_msg = next(m for m in wire if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["content"] == "file contents"


async def test_a_tool_result_precedes_new_user_text_in_the_same_turn():
    handler, seen = capture()
    req = request(
        messages=(
            base.Message(
                "user",
                (base.ToolResultBlock("call_1", "contents"), base.TextBlock("now summarise")),
            ),
        )
    )
    await backend(handler).complete(req)
    roles = [m["role"] for m in seen["body"]["messages"]]
    assert roles == ["system", "tool", "user"]


async def test_reasoning_is_dropped_on_resend_by_default():
    """Config.resend_reasoning defaults to False, and the default must be observable."""
    handler, seen = capture()
    req = request(
        messages=(base.Message("assistant", (base.ThinkingBlock("private"),)),)
    )
    await backend(handler, config=cfg(resend_reasoning=False)).complete(req)
    assert "private" not in json.dumps(seen["body"])


async def test_reasoning_is_resent_when_configured():
    handler, seen = capture()
    req = request(
        messages=(
            base.Message("assistant", (base.ThinkingBlock("private"), base.TextBlock("t"))),
        )
    )
    await backend(handler, config=cfg(resend_reasoning=True)).complete(req)
    assistant = next(m for m in seen["body"]["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "private"


# --- probe ----------------------------------------------------------------------------


async def test_probe_reads_the_served_ids_from_v1_models():
    seen: dict = {}

    def models(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": [{"id": "served-id-1"}, {"id": "other"}]})

    assert await backend(models).probe() == ("served-id-1", "other")
    assert seen["url"] == f"{HOST}/v1/models"


async def test_probe_without_a_data_list_is_a_protocol_error():
    with pytest.raises(base.BackendProtocolError, match="no 'data' list"):
        await backend(lambda req: httpx.Response(200, json={"object": "list"})).probe()


async def test_probe_on_a_dead_endpoint_is_unavailable():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(base.BackendUnavailable):
        await backend(handler).probe()


# --- the live cluster ------------------------------------------------------------------


# --------------------------------------------------------------- live-endpoint guard

UNPROVEN = ("BACKEND UNPROVEN BY THIS RUN -- this is not a pass. "
            "The live path was not exercised because ")


def _endpoint_layer(base_url: str) -> tuple[str, str]:
    """Which layer stopped us, without ever naming the host.

    Reachability is established **by address**: resolution and connection are separated
    and reported apart. ADR-0021 is about exactly this -- a single combined failure cannot
    distinguish broken DNS from a missing route, and a run that cannot tell them apart has
    no business claiming either. A hostname resolving to the *wrong* address looks
    identical to success here, which is how this endpoint was misconfigured for a week.

    The address is never returned: it is a forbidden literal, and a skip reason reaches CI
    logs. The layer is the useful part anyway.
    """
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return "config", "the configured base_url has no host"
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "dns", "the endpoint name does not resolve from this interpreter"
    family, socktype, proto, _, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(5)
    try:
        sock.connect(sockaddr)          # by address, never by name
    except OSError:
        return "route", ("the endpoint resolves but refuses or drops a connection -- "
                         "no route, or nothing listening")
    finally:
        sock.close()
    return "ok", ""


def live_model():
    """A registry entry for the live endpoint, or a loud skip saying what is unproven."""
    from claude_delegate_local import registry

    try:
        real_cfg = Config(workspace_roots=(".",), models_file="./models.toml")
        reg = registry.load(real_cfg)
    except ConfigError as e:
        pytest.skip(UNPROVEN + f"no usable registry is configured ({type(e).__name__}).")

    model = reg.resolve(None)
    layer, detail = _endpoint_layer(model.base_url)
    if layer != "ok":
        pytest.skip(UNPROVEN + f"{detail} [layer: {layer}].")
    return real_cfg, model


@pytest.mark.integration
async def test_probe_against_the_live_endpoint():
    """Skipped unless the live endpoint is genuinely reachable, and says so if not."""
    real_cfg, model = live_model()
    live = oc.OpenAICompatBackend(real_cfg, model)
    try:
        served = await live.probe()
    except base.BackendUnavailable as e:
        pytest.fail(f"reachability said ok but the probe failed: {e}")
    finally:
        await live.aclose()

    assert model.served_model_id in served, (
        f"registry names {model.served_model_id!r} but the endpoint serves {served}"
    )


async def test_reasoning_under_the_key_this_stack_actually_uses():
    """Measured, not assumed: the live server returns reasoning as "reasoning".

    v1 of the M0a-style spike found the key by reading a real response
    (JOURNAL 2026-08-26). "reasoning_content" is tolerated as a second spelling; both
    must land in the same canonical block.
    """
    for key in ("reasoning", "reasoning_content"):
        body = reply(
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "a", key: "the thinking"},
                }
            ]
        )
        # body bound as a default argument, not captured: a lambda in a loop closes over
        # the *variable*, so this happens to work only because it is called in the same
        # iteration. That is a trap for whoever restructures the loop.
        r = await backend(
            lambda req, b=body: httpx.Response(200, json=b)
        ).complete(request())
        assert r.thinking == "the thinking", key


async def test_unknown_message_keys_are_ignored_not_fatal():
    """The live server also returns refusal/annotations/audio/function_call. Tolerated."""
    body = reply(
        choices=[
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "a",
                    "refusal": None,
                    "annotations": [],
                    "audio": None,
                    "function_call": None,
                },
            }
        ]
    )
    r = await backend(lambda req: httpx.Response(200, json=body)).complete(request())
    assert r.text == "a"


# --- reasoning effort, measured against the live server -------------------------------


@pytest.mark.parametrize("level", EFFORT_LEVELS)
async def test_every_level_we_accept_maps_to_one_the_server_accepts(level):
    """The whole point of the translation table, asserted rather than assumed.

    The server validates `reasoning_effort` and refuses an unknown value with a 400
    (measured -- JOURNAL 2026-08-26). An unmapped level would therefore fail at request
    time, after the prefill had been paid for. "off" is the level that needs translating;
    this test is what stops a future edit dropping it.
    """
    handler, seen = capture()
    await backend(handler).complete(request(effort=level))
    sent = seen["body"]["reasoning_effort"]
    assert sent in oc.SERVER_EFFORT_VALUES, f"{level!r} -> {sent!r} is not a server value"


async def test_off_is_translated_to_the_servers_word_for_it():
    handler, seen = capture()
    await backend(handler).complete(request(effort="off"))
    assert seen["body"]["reasoning_effort"] == "none"


@pytest.mark.parametrize("level", ["low", "high", "max"])
async def test_the_other_levels_are_sent_verbatim(level):
    """ADR-0013: the top level is not remapped down. "max" is a real server value."""
    handler, seen = capture()
    await backend(handler).complete(request(effort=level))
    assert seen["body"]["reasoning_effort"] == level


async def test_enable_thinking_is_never_sent():
    """Tested at both polarities against the live server and changed nothing measurable."""
    handler, seen = capture()
    await backend(handler).complete(request(effort="high"))
    assert "chat_template_kwargs" not in seen["body"]


@pytest.mark.integration
async def test_one_real_completion_against_the_live_endpoint():
    """The whole round trip: canonical request out, wire, canonical response back.

    Uses effort="off" deliberately. It is the cheapest level -- reasoning is genuinely
    disabled, so the reply costs tens of tokens rather than thousands -- and it is also the
    one level whose value has to be translated, so a translation regression fails here
    against the real validator rather than only against a double.
    """
    real_cfg, model = live_model()
    live = oc.OpenAICompatBackend(real_cfg, model)
    req = base.CanonicalRequest(
        system="You answer in one short sentence.",
        messages=(base.Message("user", (base.TextBlock("Name the largest ocean."),)),),
        max_tokens=128,
        effort="off",
        temperature=0.0,
    )
    try:
        r = await live.complete(req)
    except base.BackendUnavailable as e:
        pytest.fail(f"reachability said ok but the call failed: {e}")
    finally:
        await live.aclose()

    assert r.finish_reason, "the server returned no finish_reason"
    assert r.input_tokens > 0
    assert r.output_tokens > 0
    assert r.model
    # A real answer, in real text blocks. "Pacific" is not the assertion -- having any
    # text at all, from a level that disables reasoning, is.
    assert r.text.strip(), f"no text returned; finish_reason={r.finish_reason!r}"
