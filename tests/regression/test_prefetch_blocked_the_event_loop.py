"""Prefetch ran on the event loop, so every other delegation in the process stopped with it
(PLAN U.74).

`run_delegation` called `expand_globs`, `resolve_files` and `prefetch` inline: 0.54 to 0.62s
measured over 23 to 44 files on `/mnt/c`, longer when the gitignore layer fans out. For that
long nothing else in the process was scheduled -- no heartbeat, no admission tick, no other
delegation's stream -- and handles put several runs in one process as the normal case.
"""

from __future__ import annotations

import asyncio
import time

from fastmcp import Client

from test_server import DoubleCache, cfg, chat_reply, entry, registry
from wire_double import as_stream

from claude_delegate_local import server

BLOCK_SECONDS = 0.3


def test_a_slow_prefetch_leaves_the_event_loop_free(tmp_path, monkeypatch) -> None:
    real = server.prefetch

    def slow_prefetch(*args, **kwargs):
        time.sleep(BLOCK_SECONDS)  # the blocking file work, standing in for a slow disk
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "prefetch", slow_prefetch)
    config = cfg(slots_dir=str(tmp_path), admission_idle_hold=0)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, lambda _r: as_stream(chat_reply(content="ok"))))
    ticks: list[float] = []

    async def ticker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def go():
        stop = asyncio.Event()
        beat = asyncio.create_task(ticker(stop))
        async with Client(mcp) as client:
            await client.call_tool("delegate_readonly", {"task": "q", "effort": "inherit"})
        stop.set()
        await beat

    asyncio.run(go())
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < BLOCK_SECONDS * 0.7, (
        f"the loop stood still for {max(gaps):.2f}s while prefetch ran")
