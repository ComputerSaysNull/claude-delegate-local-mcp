"""A delegation given a `workdir` was never told where it is.

The sandbox starts `run_bash` in the workdir and the file tools refuse a relative path, so a
task naming `tests/x.py` left the model to guess the directory both halves mean. Callers
worked round it by writing the path into every task, a second copy of the argument that
nothing kept in step. The rendered delegation now names the workdir, in the tail beside the
task, never in the system prompt (ADR-0011).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_delegate_local import loop, server
from test_server import DoubleCache, answered, as_stream, cfg, chat_reply, entry, registry

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="a workdir must resolve inside a workspace root, which needs a POSIX path",
)


def test_a_rendered_delegation_names_its_workdir_before_the_task():
    rendered = loop.Delegation(task="TASK", files_block="FILES", workdir="/w/repo").render()

    assert "/w/repo" in rendered
    assert rendered.index("FILES") < rendered.index("/w/repo") < rendered.index("TASK")
    assert rendered.endswith("TASK"), "the task stays last, where it varies most"


def test_a_delegation_without_a_workdir_renders_as_before():
    """Control: no workdir, no line, byte for byte what it was."""
    assert loop.Delegation(task="TASK", files_block="F").render() == "F\n\nTASK"


def test_the_system_prompts_carry_no_workdir():
    """The prompt prefix is a cached constant; per-call content must not enter it."""
    for prompt in (loop.SYSTEM_PROMPT_AGENTIC, loop.SYSTEM_PROMPT_ONE_SHOT):
        assert "{" not in prompt and "workdir is" not in prompt


def _user_messages(tmp_path: Path, **args) -> list[str]:
    """Run one `delegate` call and return the user message of every request it sent."""
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return as_stream(chat_reply(content="done"))

    config = cfg(workspace_roots=(str(tmp_path),))
    entries = (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    async def go():
        async with Client(mcp) as client:
            return await answered(
                client, "delegate", {"task": "run the tests", "effort": "low", **args}
            )

    asyncio.run(go())
    return [
        m["content"] for body in sent for m in body.get("messages", [])
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]


@posix_only
def test_the_server_names_the_workdir_it_was_given(tmp_path):
    real = os.path.realpath(tmp_path)
    users = _user_messages(tmp_path, workdir=str(tmp_path))

    assert users, "no request reached the backend"
    assert real in users[0], users[0]


@posix_only
def test_the_server_names_no_workdir_when_none_was_given(tmp_path):
    """Control: without a workdir the first user message is the task alone."""
    users = _user_messages(tmp_path)

    assert users and users[0] == "run the tests", users[:1]
