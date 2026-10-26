"""A cancelled call left the delegation it started running (ADR-0103, the stop half).

Stopping a call used to stop its work, because the work ran inside the call. Once the
delegating tools are MCP tasks, a client that cancels the task cancels the run itself --
the work stops, its admission slot is given back, and the transcript says the run was
stopped rather than leaving a delegation going on the operator's hardware.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp_tasks.client import call_tool_task

from claude_delegate_local import server
from test_server import DoubleCache, cfg, entry, models_reply, payload, registry


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def _blocking_chat():
    """A backend that serves /v1/models and /metrics but never completes a chat call."""
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=models_reply("served-id-1"))
        if request.url.path.endswith("/metrics"):
            return httpx.Response(404)
        async def body():
            await asyncio.sleep(3600)
            yield b""  # unreachable; makes this the async iterator a streamed body needs
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"})
    return handler


async def _wait_start(config, timeout=5.0):
    """Wait until the run's transcript stream has written a `start` event."""
    deadline = time.monotonic() + timeout
    while True:
        for path in Path(config.transcript_dir).glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and json.loads(line).get("t") == "start":
                    return
        if time.monotonic() >= deadline:
            raise TimeoutError("the run never started")
        await asyncio.sleep(0.05)


async def _wait_end(config, timeout=5.0):
    """Wait until the run's transcript stream has written an `end` event."""
    deadline = time.monotonic() + timeout
    while True:
        for path in Path(config.transcript_dir).glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and json.loads(line).get("t") == "end":
                    return
        if time.monotonic() >= deadline:
            raise TimeoutError("the run never ended after its task was cancelled")
        await asyncio.sleep(0.05)


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


def test_cancelling_the_task_cancels_the_run_and_frees_the_slot(tmp_path):
    config = cfg(transcript_dir=str(tmp_path))
    mcp = _mcp(config, _blocking_chat())

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(
                client, "delegate", {"task": "q", "effort": "inherit"})
            # Let the run get going before cancelling it, so this cancels a *running*
            # delegation rather than a task that had not reached its work yet.
            await _wait_start(config)
            await task.cancel()
            await _wait_end(config)
            assert _end(config)["ended"] == "stopped"
            gate = payload(await client.call_tool("backend_status", {}))["admission"]
            assert gate["inflight_seqs"] == 0, "a cancelled run must give its slot back"

    asyncio.run(go())
