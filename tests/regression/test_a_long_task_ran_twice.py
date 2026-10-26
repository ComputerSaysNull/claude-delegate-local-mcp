"""A long task was redelivered and ran twice (M21.8).

The tasks extension redelivers a task whose lease is not renewed, so a delegation that
outlives the redelivery window must have its lease renewed by the worker or it is started
again -- the model doing the work twice, on the operator's hardware. The first test pins
that a worker which renews its leases lets a long run finish exactly once; the second
disables renewal to prove the count can see a redelivery at all.
"""

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp_tasks.client import call_tool_task

from claude_delegate_local import server
from test_server import DoubleCache, cfg, entry, models_reply, registry
from wire_double import stream_reply


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def _slow_handler(seconds=2.5):
    """A chat backend that takes `seconds` before answering, serving /v1/models at once."""
    body_text = stream_reply(content="the answer")

    async def body():
        await asyncio.sleep(seconds)
        yield body_text.encode()

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=models_reply("served-id-1"))
        if request.url.path.endswith("/metrics"):
            return httpx.Response(404)
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"})

    return handler


def _count(config, kind):
    """How many `kind` events the run's transcript stream wrote."""
    count = 0
    for path in Path(config.transcript_dir).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and json.loads(line).get("t") == kind:
                count += 1
    return count


async def _wait_starts(config, n, timeout=15.0):
    """Wait until the transcript shows the run was started at least `n` times."""
    deadline = time.monotonic() + timeout
    while True:
        if _count(config, "start") >= n:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"the run was not started {n} times within {timeout}s")
        await asyncio.sleep(0.05)


async def _wait_end(config, timeout=15.0):
    """Wait until the run's transcript stream has written an `end` event."""
    deadline = time.monotonic() + timeout
    while True:
        for path in Path(config.transcript_dir).glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and json.loads(line).get("t") == "end":
                    return
        if time.monotonic() >= deadline:
            raise TimeoutError("the run never ended within the redelivery window")
        await asyncio.sleep(0.05)


def test_a_long_run_is_started_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_TASK_REDELIVERY", timedelta(seconds=0.5), raising=False)
    config = cfg(transcript_dir=str(tmp_path))
    mcp = _mcp(config, _slow_handler())

    async def go():
        async with Client(mcp) as client:
            await call_tool_task(client, "delegate", {"task": "q", "effort": "inherit"})
            await _wait_end(config)

    asyncio.run(go())
    assert _count(config, "start") == 1, (
        "a worker that renews its leases must not let a run be started twice")


def test_a_run_whose_lease_is_never_renewed_is_redelivered(tmp_path, monkeypatch):
    import docket.worker

    async def _never(self, *args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "_TASK_REDELIVERY", timedelta(seconds=0.5), raising=False)
    monkeypatch.setattr(docket.worker.Worker, "_renew_leases", _never)
    config = cfg(transcript_dir=str(tmp_path))
    mcp = _mcp(config, _slow_handler())

    async def go():
        async with Client(mcp) as client:
            await call_tool_task(client, "delegate", {"task": "q", "effort": "inherit"})
            await _wait_starts(config, 2)

    asyncio.run(go())
    assert _count(config, "start") > 1, (
        "with its lease never renewed the run must be started again, not left to finish once")
