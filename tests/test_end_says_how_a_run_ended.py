"""The transcript's `end` event names how a run ended, in `ended`.

`ok` and `error` say whether a run failed and, in words, why; a reader that may not parse
those words (they are not contract) needs which of six endings it was. One run per
ending, driven end to end through a real MCP session and a transport double, plus the
format bump (1.4) the field comes with.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from claude_delegate_local import server, transcript
from claude_delegate_local.admission import AdmissionTimedOut
from test_server import (
    DoubleCache,
    cfg,
    chat_handler,
    chat_reply,
    entry,
    payload,
    registry,
    serves_metrics,
    tool_call_reply,
    answered,
)
from wire_double import as_stream, delta


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def _end(config):
    """The last `end` event the run's transcript stream wrote."""
    events = []
    for path in Path(config.transcript_dir).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                events.append(json.loads(line))
    ends = [e for e in events if e.get("t") == "end"]
    assert ends, f"no end event; the stream wrote {[e.get('t') for e in events]}"
    return ends[-1]


def _scripted(*replies):
    """One canned chat reply per request, with the metrics scrape answered separately."""
    remaining = list(replies)

    def inner(request):
        return as_stream(remaining.pop(0))

    return serves_metrics(inner)


async def _start_and_finish(client, **args):
    """Start a delegation and wait for it, swallowing the ToolError a failed run raises."""
    args.setdefault("effort", "inherit")
    try:
        return await answered(client, "delegate", args)
    except ToolError:
        return None


def test_a_finished_run_says_finished(tmp_path):
    config = cfg(transcript_dir=str(tmp_path))
    mcp = _mcp(config, chat_handler(content="done"))

    async def go():
        async with Client(mcp) as client:
            await _start_and_finish(client, task="q")

    asyncio.run(go())
    end = _end(config)
    assert end["ok"] is True
    assert end["ended"] == "finished"


def test_a_cancelled_run_says_stopped(tmp_path):
    """Pause the run on `ask_caller`, then stop it with `cancel_delegation`."""
    config = cfg(transcript_dir=str(tmp_path))
    handler = _scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="never reached"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            handle = started["handle"]
            asked = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert asked["status"] == "question", asked
            stopped = payload(await client.call_tool(
                "cancel_delegation", {"handle": handle}))
            assert stopped["stopped"] is True, stopped
            after = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 1}))
            assert after["status"] == "cancelled", after

    asyncio.run(go())
    assert _end(config)["ended"] == "stopped"


def test_an_admission_timeout_says_queue_timeout(tmp_path, monkeypatch):
    class NeverAdmits(server.Admission):
        @asynccontextmanager
        async def admit(self, *args, **kwargs):
            raise AdmissionTimedOut(0.0, "queued_behind_earlier_waiters", 1)
            yield  # unreachable; makes this the async generator a context manager needs

    monkeypatch.setattr(server, "Admission", NeverAdmits)
    config = cfg(transcript_dir=str(tmp_path), slots_dir=str(tmp_path / "slots"))
    mcp = _mcp(config, chat_handler())

    async def go():
        async with Client(mcp) as client:
            await _start_and_finish(client, task="q")

    asyncio.run(go())
    assert _end(config)["ended"] == "queue_timeout"


def _never_ends(seconds: float):
    """A streaming backend that never finishes: it just keeps delivering `a` deltas."""
    async def body():
        while True:
            yield ("data: " + json.dumps(delta(content="a")) + "\n\n").encode()
            await asyncio.sleep(seconds)

    def handler(request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"})

    return serves_metrics(handler)


def test_a_run_past_its_deadline_says_deadline(tmp_path):
    """Tokens keep arriving, so no stall -- but the delegation never finishes."""
    config = cfg(transcript_dir=str(tmp_path), slots_dir=str(tmp_path / "slots"),
                 connect_timeout=1, stall_timeout=1, dispatch_timeout=2)
    mcp = _mcp(config, _never_ends(0.02))

    async def go():
        async with Client(mcp) as client:
            await _start_and_finish(
                client, task="q", allowed_tools=[])

    asyncio.run(go())
    assert _end(config)["ended"] == "deadline"


def _goes_silent():
    """A streaming backend that accepts the call and then produces nothing."""
    async def body():
        while True:
            await asyncio.sleep(1000)
        yield b""  # unreachable; makes this the async iterator a streamed body needs

    def handler(request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"})

    return serves_metrics(handler)


def test_a_stalled_run_says_stalled(tmp_path):
    config = cfg(transcript_dir=str(tmp_path), slots_dir=str(tmp_path / "slots"),
                 connect_timeout=1, stall_timeout=1, dispatch_timeout=2)
    mcp = _mcp(config, _goes_silent())

    async def go():
        async with Client(mcp) as client:
            await _start_and_finish(
                client, task="q", allowed_tools=[])

    asyncio.run(go())
    assert _end(config)["ended"] == "stalled"


def test_an_unreachable_backend_says_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("connection failed")

    config = cfg(transcript_dir=str(tmp_path), slots_dir=str(tmp_path / "slots"))
    mcp = _mcp(config, serves_metrics(handler))

    async def go():
        async with Client(mcp) as client:
            await _start_and_finish(
                client, task="q", allowed_tools=[])

    asyncio.run(go())
    assert _end(config)["ended"] == "error"


def test_the_schema_lists_the_endings_since_1_4():
    """The `ended` values are a 1.4 addition (ADR-0111), so the format is at least 1.4."""
    schema = json.loads(
        Path(transcript.__file__).with_name("transcript.schema.json").read_text(
            encoding="utf-8"))
    enum = schema["$defs"]["end"]["properties"]["ended"]["enum"]
    assert set(enum) == {
        "finished", "stopped", "queue_timeout", "deadline", "stalled", "error",
    }
    major, minor = (int(part) for part in transcript.FORMAT.split("."))
    assert (major, minor) >= (1, 4)
