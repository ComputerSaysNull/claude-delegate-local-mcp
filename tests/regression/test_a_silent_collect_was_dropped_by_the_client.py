"""A `collect` waiting on a long run said nothing, and the client dropped it (PLAN U.89).

The client abandons a call that sends nothing for its stdio idle timeout (ADR-0018), and
`wait_seconds` invited the whole remaining run. A 3500s wait was dropped with no response
while the run it waited on finished fine. The delegating tools already beat through their
own silence; `collect` had no context to beat with.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

from test_server import DoubleCache, cfg, entry, payload, registry, slow_chat_handler

from claude_delegate_local import server


def _progress_during_collect(tmp_path, *, run_seconds: float, wait_seconds: float):
    config = cfg(slots_dir=str(tmp_path), admission_idle_hold=0, keepalive_interval=1)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, slow_chat_handler(run_seconds)))
    seen: list[float] = []

    async def on_progress(progress, total, message):
        seen.append(progress)

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "t", "effort": "inherit", "allowed_tools": []}))
            # The writing call answers with a handle and reports nothing, so every
            # notification from here on is `collect`'s own.
            seen.clear()
            return payload(await client.call_tool(
                "collect", {"handle": started["handle"], "wait_seconds": wait_seconds}))

    return asyncio.run(go()), seen


def test_a_long_collect_keeps_the_client_informed(tmp_path) -> None:
    result, seen = _progress_during_collect(tmp_path, run_seconds=2.5, wait_seconds=10)
    assert result["status"] == "done"
    assert len(seen) >= 2, (
        f"a 2.5s wait past a 1s keepalive sent {len(seen)} notification(s)")
    assert seen == sorted(set(seen)), f"progress must rise every time, got {seen}"


def test_a_collect_that_returns_at_once_sends_nothing(tmp_path) -> None:
    """The control: a finished run is answered without a single notification."""
    result, seen = _progress_during_collect(tmp_path, run_seconds=0, wait_seconds=10)
    assert result["status"] == "done"
    assert seen == []
