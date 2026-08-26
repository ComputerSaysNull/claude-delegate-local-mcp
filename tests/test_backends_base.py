"""The canonical shape holds its own invariants, and refuses at construction.

Every rejection below has a paired assertion that it *fires*. A validator that cannot
fail is worse than none, because it is trusted -- three such checks have already been
found in this repository.
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_delegate_local.backends import base
from claude_delegate_local.config import EFFORT_LEVELS


def msg(text: str = "hello") -> base.Message:
    return base.Message("user", (base.TextBlock(text),))


def request(**over: object) -> base.CanonicalRequest:
    kw: dict = {
        "system": "static system prompt",
        "messages": (msg(),),
        "max_tokens": 1024,
        "effort": "low",
        "temperature": 0.0,
    }
    kw.update(over)
    return base.CanonicalRequest(**kw)  # type: ignore[arg-type]


# --- Message ---------------------------------------------------------------------------


def test_message_accepts_a_tuple_of_blocks():
    m = base.Message("assistant", (base.TextBlock("a"), base.ThinkingBlock("b")))
    assert m.role == "assistant"
    assert len(m.content) == 2


def test_message_rejects_a_bare_string():
    """ADR-0008 condition (a). A coerced string is the canonical shape failing quietly."""
    with pytest.raises(base.CanonicalShapeError, match="not a string"):
        base.Message("user", "hello")  # type: ignore[arg-type]


def test_message_rejects_a_list_even_of_valid_blocks():
    with pytest.raises(base.CanonicalShapeError, match="must be a tuple"):
        base.Message("user", [base.TextBlock("a")])  # type: ignore[arg-type]


def test_message_rejects_an_unknown_role():
    with pytest.raises(base.CanonicalShapeError, match="not one of"):
        base.Message("system", (base.TextBlock("a"),))


def test_message_rejects_the_openai_tool_role():
    """A tool result is a block on a user message here, not a role. The adapter maps it."""
    with pytest.raises(base.CanonicalShapeError, match="ToolResultBlock"):
        base.Message("tool", (base.TextBlock("a"),))


def test_message_rejects_a_non_block_element():
    with pytest.raises(base.CanonicalShapeError, match="is not a content block"):
        base.Message("user", ({"type": "text", "text": "a"},))  # type: ignore[arg-type]


def test_blocks_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        base.TextBlock("a").text = "b"  # type: ignore[misc]


# --- CanonicalRequest ------------------------------------------------------------------


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_request_accepts_every_level_config_defines(level):
    """The accepted set is config's, by import. Not a second tuple that can drift."""
    assert request(effort=level).effort == level


def test_request_rejects_an_effort_config_does_not_define():
    with pytest.raises(base.CanonicalShapeError, match="not one of"):
        request(effort="medium")


def test_request_carries_no_generation_defaults():
    """Config defaults live only in config.py; a default here would be a second copy.

    Asserted structurally rather than by eye, because the drift this guards against is
    exactly the kind that survives review.
    """
    fields = {f.name: f for f in dataclasses.fields(base.CanonicalRequest)}
    for name in ("max_tokens", "effort", "temperature"):
        assert fields[name].default is dataclasses.MISSING, (
            f"{name} has a default in CanonicalRequest; defaults belong in config.py"
        )
        assert fields[name].default_factory is dataclasses.MISSING


def test_request_rejects_empty_messages():
    with pytest.raises(base.CanonicalShapeError, match="nothing to send"):
        request(messages=())


def test_request_rejects_a_list_of_messages():
    with pytest.raises(base.CanonicalShapeError, match="must be a tuple"):
        request(messages=[msg()])


def test_request_rejects_a_non_message_element():
    with pytest.raises(base.CanonicalShapeError, match="not a Message"):
        request(messages=({"role": "user", "content": "hi"},))


@pytest.mark.parametrize("bad", [0, -1])
def test_request_rejects_a_non_positive_max_tokens(bad):
    with pytest.raises(base.CanonicalShapeError, match="at least 1"):
        request(max_tokens=bad)


@pytest.mark.parametrize("bad", [-0.1, 2.1])
def test_request_rejects_a_temperature_outside_the_range(bad):
    with pytest.raises(base.CanonicalShapeError, match="outside the accepted range"):
        request(temperature=bad)


def test_request_defaults_to_no_tools():
    assert request().tools == ()


# --- CanonicalResponse -----------------------------------------------------------------


def test_null_content_at_length_is_a_response_not_an_error():
    """ADR-0014's failure, pinned. M3 decides what it means; this layer just reports it.

    Turning this into an exception would move the decision into the wrong layer, and it
    would do so silently -- hence a test rather than a comment.
    """
    r = base.CanonicalResponse(
        content=(), finish_reason="length", input_tokens=11, output_tokens=2048, model="m"
    )
    assert r.text == ""
    assert r.tool_uses == ()
    assert r.finish_reason == "length"
    assert r.output_tokens == 2048


def test_response_finish_reason_is_not_translated():
    """Whatever the wire said, verbatim. Mapping it would be interpretation."""
    for wire in ("stop", "length", "tool_calls", "something_new_upstream_invented"):
        assert base.CanonicalResponse((), wire, 0, 0, "m").finish_reason == wire


def test_response_accessors_partition_the_blocks():
    r = base.CanonicalResponse(
        content=(
            base.ThinkingBlock("because"),
            base.TextBlock("hello "),
            base.TextBlock("world"),
            base.ToolUseBlock("call_1", "read_file", {"path": "a.py"}),
        ),
        finish_reason="tool_calls",
        input_tokens=1,
        output_tokens=2,
        model="m",
    )
    assert r.text == "hello world"
    assert r.thinking == "because"
    assert [b.name for b in r.tool_uses] == ["read_file"]


# --- errors ----------------------------------------------------------------------------


def test_refused_carries_the_status_and_body_for_feature_detection():
    """ADR-0017: only the live body says which flag actually gates the field."""
    e = base.BackendRefused(400, "thinking_token_budget requires VLLM_SOMETHING=0", "/v1/chat")
    assert e.status == 400
    assert "VLLM_SOMETHING=0" in e.body
    assert "VLLM_SOMETHING=0" in str(e)
    assert "/v1/chat" in str(e)


def test_every_error_kind_is_distinguishable_from_the_others():
    kinds = [
        base.CanonicalShapeError,
        base.BackendUnavailable,
        base.BackendRefused,
        base.BackendProtocolError,
    ]
    for kind in kinds:
        assert issubclass(kind, base.BackendError)
        others = [k for k in kinds if k is not kind]
        for other in others:
            assert not issubclass(kind, other), f"{kind.__name__} is a {other.__name__}"


# --- the protocol ----------------------------------------------------------------------


def test_protocol_accepts_a_complete_implementation():
    class Stub:
        async def complete(self, request):
            ...

        async def probe(self):
            ...

        async def aclose(self):
            ...

    assert isinstance(Stub(), base.Backend)


def test_protocol_rejects_an_incomplete_implementation():
    class Partial:
        async def complete(self, request):
            ...

    assert not isinstance(Partial(), base.Backend)
