"""A queued delegation wrote a `waiting` event on every quarter-second poll.

Eight calls held forty minutes in admission wrote 7.7 MB, a quarter of the transcript
directory, into a synced folder. The client does need a notification on every tick -- that
is what keeps its idle timer from abandoning a queued call -- but the transcript does not:
the viewer shows one line a minute, and the age it shows is the event's own cumulative
`waited_seconds`. So the stream is thinned and the wire is not.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import Client

from test_server import DoubleCache, cfg, chat_reply, entry, registry
from wire_double import as_stream

from claude_delegate_local import server

TICKS = 40


def _events(directory: Path) -> list[dict]:
    lines = [ln for p in directory.glob("*.jsonl") for ln in p.read_text("utf-8").splitlines()]
    return [json.loads(ln) for ln in lines if ln]


def test_a_queued_delegation_writes_few_waiting_events_but_notifies_every_tick(
    tmp_path, monkeypatch,
) -> None:
    class PollingGate(server.Admission):
        """Queued for TICKS polls, much faster than any real one, then admitted."""

        @asynccontextmanager
        async def admit(self, *args, on_wait=None, **kwargs):
            for _ in range(TICKS):
                await asyncio.sleep(0.001)
                if on_wait is not None:
                    await on_wait()
            async with super().admit(*args, on_wait=on_wait, **kwargs) as lease:
                yield lease

    monkeypatch.setattr(server, "Admission", PollingGate)
    config = cfg(transcript_dir=str(tmp_path), slots_dir=str(tmp_path / "slots"),
                 admission_idle_hold=0)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, lambda _r: as_stream(chat_reply(content="done"))))
    notified: list[float] = []

    async def on_progress(progress, total, message):
        notified.append(progress)

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            await client.call_tool("delegate_readonly", {"task": "t", "effort": "inherit"})

    asyncio.run(go())
    waiting = [e for e in _events(tmp_path) if e.get("t") == "waiting"]
    assert 1 <= len(waiting) <= 2, f"{len(waiting)} waiting events for {TICKS} polls"
    assert len(notified) >= TICKS, "the client stopped being told on every tick"
