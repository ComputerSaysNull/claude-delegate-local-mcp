"""A refused tool call's message reaches the record, and a successful one's does not.

The record exists to answer "why did that call fail" -- ADR-0007's rule that the server
reports what it watched, and the one question a record that dropped the refusal text could
not answer. So the refusal text is carried on the error outcome, and deliberately not on a
success: a successful `read_file`'s result *is* the file body, which is exactly what
ADR-0039 keeps out of the record. The negative direction is the one worth testing hardest,
because a check that always filled `message` would pass the first test and still be wrong.

The cap is the other half. A refusal is small and exists nowhere else once the delegation
ends, but "small" is not "unbounded" -- a refusal that is itself the size of the payload it
exists to explain would recreate the problem the record was built to avoid.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry
from claude_delegate_local.tools import ToolRefused

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist
BULK = "y" * 20_000  # a result with enough bytes for the accounting to be visible
LONG_PATH = "x" * 700  # long enough that the refusal text exceeds TOOL_MESSAGE_CAP


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def reads(path: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock("looking"), ToolUseBlock(id=f"c-{path}", name="reader",
                                                    input={"path": path})),
        finish_reason="tool_calls",
        input_tokens=100,
        output_tokens=5,
        model="served-id-1",
    )


def refuses(path: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock("trying"), ToolUseBlock(id=f"c-{path}", name="refuser",
                                                    input={"path": path})),
        finish_reason="tool_calls",
        input_tokens=100,
        output_tokens=5,
        model="served-id-1",
    )


def answers(text: str = "done") -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=100,
        output_tokens=5,
        model="served-id-1",
    )


class Scripted:
    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)

    async def complete(self, request):
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


@pytest.fixture
def reader():
    """A tool taking a `path`, so the success direction has something to key on.

    Not cacheable: dedup would serve the second read of the same path from memory, and a
    re-read that never happened is not the thing being measured.
    """
    added = {
        "reader": tools.RegisteredTool(
            spec=ToolSpec(name="reader", description="read", input_schema={"type": "object"}),
            handler=lambda cfg_, args: BULK,
            cacheable=False,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def _refuse(cfg_, args) -> str:
    raise ToolRefused(f"refused: {args['path']}")


@pytest.fixture
def refuser():
    """A tool that always refuses, so the record has a refusal text to carry."""
    added = {
        "refuser": tools.RegisteredTool(
            spec=ToolSpec(name="refuser", description="refuse", input_schema={"type": "object"}),
            handler=_refuse,
            cacheable=False,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def run(*replies, diagnostics: bool = False, **over):
    return asyncio.run(
        loop.run_agentic_loop(
            cfg(**over),
            ModelEntry(key="flash", base_url=HOST, served_model_id="served-id-1"),
            Scripted(*replies),
            loop.Delegation("do the thing"),
            allowed=frozenset({"reader", "refuser"}),
            max_turns=8,
            diagnostics=diagnostics,
        )
    )


# --- the refusal text reaches the record ------------------------------------------------------


def test_a_refused_call_records_its_refusal_text_in_the_message(refuser):
    """The record exists to answer why a call failed, so the refusal must survive in it."""
    result = run(refuses("/a.py"), answers(), diagnostics=True)
    record = result.diagnostics[0].tool_calls[0]
    assert (record.name, record.outcome) == ("refuser", "error")
    assert record.message == "refused: /a.py"


def test_a_successful_call_records_an_empty_message(reader):
    """The other direction, so the check above cannot be one that always passes.

    A successful `read_file`'s result is the file body, which ADR-0039 keeps out of the
    record; the record carries accounting instead, and `message` stays empty.
    """
    result = run(reads("/a.py"), answers(), diagnostics=True)
    record = result.diagnostics[0].tool_calls[0]
    assert (record.name, record.outcome) == ("reader", "ran")
    assert record.message == ""


def test_a_refusal_longer_than_the_cap_is_truncated_and_says_how_much_was_dropped(refuser):
    """The cap keeps a refusal from becoming the payload it exists to explain.

    The elision marker is not decoration: a silently truncated refusal reads as a complete
    one, so a reader diagnosing the call would draw conclusions from a reason that was
    never the whole reason.
    """
    text = f"refused: {LONG_PATH}"
    result = run(refuses(LONG_PATH), answers(), diagnostics=True)
    record = result.diagnostics[0].tool_calls[0]
    dropped = len(text) - loop.TOOL_MESSAGE_CAP
    assert record.message == text[:loop.TOOL_MESSAGE_CAP] + f"... [+{dropped} chars]"
    assert record.message.endswith(f"... [+{dropped} chars]")
