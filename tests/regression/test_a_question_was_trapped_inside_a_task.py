"""A run that asked a question trapped it behind a `collect` (ADR-0118).

A write-capable delegation returned a handle at once, and a caller that wanted the
question had to `collect` to learn the run was waiting on an answer. Once the delegating
tools are MCP tasks, a run that asks ends its task with the question, its `questions` and
its `handle` as the task result, and `answer` is a task that continues the *same* run --
so the run is started once and ended once, not once per call.
"""

import asyncio
import json
import time
from pathlib import Path

from fastmcp import Client
from fastmcp_tasks.client import call_tool_task

from claude_delegate_local import server
from test_server import (
    DoubleCache,
    cfg,
    chat_reply,
    entry,
    payload,
    registry,
    serves_metrics,
    tool_call_reply,
)
from wire_double import as_stream


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def _scripted(*replies):
    """One canned chat reply per request, with the metrics scrape answered separately."""
    remaining = list(replies)

    def inner(request):
        return as_stream(remaining.pop(0))

    return serves_metrics(inner)


async def _terminal(task, timeout=15.0):
    """Wait until the task is no longer working, then return its status."""
    deadline = time.monotonic() + timeout
    while True:
        state = await task.status()
        if state.status in ("completed", "failed", "cancelled", "input_required"):
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError(f"task still {state.status} after {timeout}s")
        await asyncio.sleep(0.05)


def _events(config):
    """Every stream event the run wrote, in order, across its .jsonl file(s)."""
    events = []
    for path in Path(config.transcript_dir).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                events.append(json.loads(line))
    return events


def test_a_question_is_the_task_result_and_answer_resumes_the_same_run(tmp_path):
    config = cfg(transcript_dir=str(tmp_path))
    handler = _scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            delegate_task = await call_tool_task(
                client, "delegate", {"task": "q", "effort": "inherit"})
            await _terminal(delegate_task)
            asked = payload(await delegate_task.result())
            assert asked["status"] == "question", asked
            assert asked["questions"] == ["Which file?"]
            handle = asked["handle"]

            answer_task = await call_tool_task(
                client, "answer", {"handle": handle, "text": "foo.py"})
            await _terminal(answer_task)
            return payload(await answer_task.result())

    result = asyncio.run(go())
    assert result["answer"] == "it is foo.py"

    kinds = [event["t"] for event in _events(config)]
    assert kinds.count("start") == 1, "answer must continue the same run, not start a new one"
    assert kinds.count("end") == 1
