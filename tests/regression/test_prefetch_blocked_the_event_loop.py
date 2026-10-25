"""Prefetch ran on the event loop, so every other delegation in the process stopped with it
(PLAN U.74).

`run_delegation` called `expand_globs`, `resolve_files` and `prefetch` inline: 0.54 to 0.62s
measured over 23 to 44 files on `/mnt/c`, longer when the gitignore layer fans out. For that
long nothing else in the process was scheduled -- no heartbeat, no admission tick, no other
delegation's stream -- and handles put several runs in one process as the normal case.

The test used to time the loop's ticks, which measured the machine rather than the prefetch
and failed under a full suite's load (PLAN U.111); it now hands off between the two -- the
fake prefetch waits for a coroutine that can only run if the loop is free.
"""

import asyncio
import threading

from fastmcp import Client

from test_server import DoubleCache, cfg, chat_reply, entry, registry
from wire_double import as_stream

from claude_delegate_local import server

# Only spent when the loop is blocked, i.e. when the test is failing anyway.
HANDSHAKE_SECONDS = 10.0


def test_a_slow_prefetch_leaves_the_event_loop_free(tmp_path, monkeypatch) -> None:
    real = server.prefetch
    entered = threading.Event()
    released = threading.Event()
    blocked: list[bool] = []

    def slow_prefetch(*args, **kwargs):
        entered.set()
        blocked.append(not released.wait(HANDSHAKE_SECONDS))
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "prefetch", slow_prefetch)
    config = cfg(slots_dir=str(tmp_path), admission_idle_hold=0)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, lambda _r: as_stream(chat_reply(content="ok"))))

    async def release() -> None:
        while not entered.is_set():
            await asyncio.sleep(0.01)
        released.set()

    async def go():
        releaser = asyncio.create_task(release())
        async with Client(mcp) as client:
            await client.call_tool("delegate_readonly", {"task": "q", "effort": "inherit"})
        releaser.cancel()  # still polling only if prefetch was never called, which fails below

    asyncio.run(go())
    assert blocked == [False], (
        "prefetch held the event loop: a coroutine on it could not run "
        "until the handshake timed out")
