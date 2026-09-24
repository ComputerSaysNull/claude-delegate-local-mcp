"""Progress notifications went backwards, and claimed a total of zero.

Turns reported `progress(turn, of)` and the heartbeats -- a queued tick, the keepalive --
reported `progress(0, 0)`, so a call's sequence read 0, 0, 1, 0, 2. The MCP specification
requires the value to increase with each notification. Claude Code only uses them to reset
its idle timer, so it worked here, but a stricter client may drop the stream, and `total=0`
reads as "zero of zero". One counter per call now rises on every notification, the total
is left out because it is not known, and what happened goes in `message`.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from itertools import pairwise

from fastmcp import Client

from test_server import DoubleCache, cfg, chat_reply, entry, registry, tool_call_reply, turn_handler

from claude_delegate_local import server


def test_progress_only_rises_across_queued_ticks_and_turns(tmp_path, monkeypatch) -> None:
    class PollingGate(server.Admission):
        @asynccontextmanager
        async def admit(self, *args, on_wait=None, **kwargs):
            for _ in range(3):
                await asyncio.sleep(0.001)
                if on_wait is not None:
                    await on_wait()
            async with super().admit(*args, on_wait=on_wait, **kwargs) as lease:
                yield lease

    monkeypatch.setattr(server, "Admission", PollingGate)
    config = cfg(max_turns_default=4, slots_dir=str(tmp_path), admission_idle_hold=0)
    mcp = server.build(config, registry(entry()), DoubleCache(config, turn_handler(
        tool_call_reply("read_file", {"path": "/nowhere/at/all.py"}),
        chat_reply(content="I could not read it"),
    )))
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            await client.call_tool("delegate", {"task": "t", "effort": "inherit"})

    asyncio.run(go())
    values = [p for p, _, _ in seen]
    assert len(values) >= 5, seen
    assert all(b > a for a, b in pairwise(values)), f"progress went backwards: {values}"
    assert all(t is None for _, t, _ in seen), "a total nobody knows was reported"
    messages = [m for _, _, m in seen]
    assert any(m and m.startswith("queued") for m in messages), messages
    assert "turn 2 of 4" in messages, messages
