"""Withdrawing the tools on the final turn cost the entire prefix, to save the tool block.

`final = turn == turns` withdrew `tools` so the model could only produce an answer. The
intent was right; the mechanism changed the *front* of the prompt, and the serving stack
caches prefixes.

Measured on the live cluster, one history, four arms, nothing evicted and nothing else
varying:

    A   tools present                        prompt 36,339   cached 36,096   99.3%
    B   tools present + tool_choice: none    prompt 36,339   cached 36,096   99.3%
    C   tools withdrawn (the old final turn) prompt 36,018   cached      0    0.0%
    A'  tools present, repeated              prompt 36,339   cached 36,096   99.3%

C re-prefilled 36,018 tokens to avoid sending 321 tokens of tool schema, and A' afterwards
proves the cache was still warm rather than evicted by C. B is byte-identical to A, so the
chat template renders the tool block the same way when calls are forbidden -- which is the
thing that had to be checked before this fix was worth building, because a template that
dropped the block under `tool_choice: "none"` would have reintroduced the same divergence.

This lands on the turn that can least afford it: the tools-withdrawn turn is also the one
that must fit a whole answer inside one deadline (ADR-0055), so it paid a full cold prefill
first. ADR-0057.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.backends.base import (
    CanonicalRequest,
    Message,
    TextBlock,
    ToolSpec,
)
from claude_delegate_local.backends.openai_compat import OpenAICompatBackend
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

TOOLS = (
    ToolSpec(name="read_file", description="Read a file.",
             input_schema={"type": "object", "properties": {"path": {"type": "string"}}}),
)


def _backend() -> OpenAICompatBackend:
    return OpenAICompatBackend(
        Config(workspace_roots=(".",)),  # type: ignore[arg-type]
        ModelEntry(key="flash", base_url="http://example.com:8000",
                   served_model_id="served-id-1"),  # type: ignore[arg-type]
    )


def _request(**over) -> CanonicalRequest:
    kw = {
        "system": "s",
        "messages": (Message("user", (TextBlock("hello"),)),),
        "max_tokens": 16,
        "effort": "off",
        "temperature": 0.0,
        "tools": TOOLS,
    }
    kw.update(over)
    return CanonicalRequest(**kw)  # type: ignore[arg-type]


def test_forbidding_calls_leaves_the_tool_block_byte_identical() -> None:
    """The property the cache depends on, asserted on the wire body rather than inferred.

    Everything the prompt is built from must match the turn before it. If `tool_choice`
    ever changed the tool block, the prefix would diverge and this fix would be undone
    silently -- which is exactly how the bug it replaces behaved.
    """
    allowed = _backend().wire_body(_request(tool_choice="auto"))
    forbidden = _backend().wire_body(_request(tool_choice="none"))

    assert forbidden["tools"] == allowed["tools"]
    assert forbidden["messages"] == allowed["messages"]
    assert forbidden["tool_choice"] == "none"
    # "auto" is our default and is left off the wire, so an ordinary turn is unchanged.
    assert "tool_choice" not in allowed


def test_withdrawing_the_tools_is_what_moved_the_prefix() -> None:
    """The control, stating the defect this file is named for.

    If the tool block ever stopped being part of the body, the measurement above would
    stop meaning anything and so would the fix.
    """
    withdrawn = _backend().wire_body(_request(tools=()))

    assert "tools" not in withdrawn
    assert "tool_choice" not in withdrawn


def test_tool_choice_is_only_sent_where_there_are_tools_to_forbid() -> None:
    """A statement about tools, with no tools, is noise the cache would still pay for."""
    assert "tool_choice" not in _backend().wire_body(_request(tools=(), tool_choice="none"))


@pytest.mark.parametrize("bad", ["required", "READ_FILE", "", "any"])
def test_an_unknown_tool_choice_is_refused_before_dispatch(bad: str) -> None:
    """Our vocabulary is two words and each adapter translates it, exactly as for effort.
    An unlisted one has no translation, so it is refused here rather than 400'd after a
    prefill has been paid for."""
    with pytest.raises(ValueError, match="tool_choice"):
        _request(tool_choice=bad)
