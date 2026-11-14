"""A run killed by a deadline records the turns it had completed, not `null`.

`DispatchTimedOut` carries `turns` for the one reason ADR-0078 gives: a run abandoned
mid-work must say what it had done. `dispatch_delegation` converts that exception into a
`ToolError` and copies the counters onto it, so by the time `delegate()`'s `finally` writes
the transcript's `end` event the failure is a `ToolError`, never a `DispatchTimedOut`. The
`isinstance(failure, DispatchTimedOut)` test there is therefore never true, and every
stalled or past-deadline run recorded `turns: null` -- the counters lost on the one
conversion that was written to keep them.

Both endings are driven here through a real MCP session: one turn is completed by a real
`read_file` call, and the next turn stalls or runs past the deadline. The turn count the
`end` event must carry is that one completed turn.
"""

import asyncio
import json
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from claude_delegate_local import server
from test_server import (
    DoubleCache,
    answered,
    cfg,
    entry,
    registry,
    serves_metrics,
    tool_call_reply,
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


def _first_turn_calls_a_tool(target):
    """The reply for turn one: a real `read_file`, so the turn completes and the loop
    goes on to a second one."""
    return tool_call_reply("read_file", {"path": str(target)})


def _never_ends(seconds: float):
    """Every request after the first keeps delivering `a` deltas and never finishes."""
    async def body():
        while True:
            yield ("data: " + json.dumps(delta(content="a")) + "\n\n").encode()
            await asyncio.sleep(seconds)

    def handler(request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"})

    return handler


def _goes_silent():
    """Every request after the first accepts the call and then produces nothing."""
    async def body():
        while True:
            await asyncio.sleep(1000)
        yield b""  # unreachable; makes this the async iterator a streamed body needs

    def handler(request):
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"})

    return handler


def _turn_one_then(first_reply, later):
    """Answer the first chat request with `first_reply`, and every later one with `later`.

    The metrics scrape is answered before this, by the `serves_metrics` wrapper, so it
    cannot consume the canned turn-one reply and turn one really completes.
    """
    remaining = [first_reply]

    def handler(request):
        if remaining:
            return as_stream(remaining.pop(0))
        return later(request)

    return serves_metrics(handler)


def test_a_stalled_run_records_the_turn_it_completed(tmp_path):
    target = tmp_path / "note.py"
    target.write_text("# hello\n", encoding="utf-8")
    config = cfg(
        transcript_dir=str(tmp_path / "transcripts"),
        slots_dir=str(tmp_path / "slots"),
        workspace_roots=(str(tmp_path),),
        connect_timeout=1, stall_timeout=1, dispatch_timeout=10,
    )
    mcp = _mcp(config, _turn_one_then(_first_turn_calls_a_tool(target), _goes_silent()))

    async def go():
        async with Client(mcp) as client:
            try:
                await answered(client, "delegate", {
                    "task": "q", "effort": "inherit",
                    "allowed_tools": ["read_file"], "max_turns": 4,
                })
            except ToolError:
                pass

    asyncio.run(go())
    end = _end(config)
    assert end["ended"] == "stalled", end
    assert end["turns"] == 1, end


def test_a_run_past_its_deadline_records_the_turn_it_completed(tmp_path):
    target = tmp_path / "note.py"
    target.write_text("# hello\n", encoding="utf-8")
    config = cfg(
        transcript_dir=str(tmp_path / "transcripts"),
        slots_dir=str(tmp_path / "slots"),
        workspace_roots=(str(tmp_path),),
        connect_timeout=1, stall_timeout=1, dispatch_timeout=2,
    )
    mcp = _mcp(config, _turn_one_then(_first_turn_calls_a_tool(target), _never_ends(0.02)))

    async def go():
        async with Client(mcp) as client:
            try:
                await answered(client, "delegate", {
                    "task": "q", "effort": "inherit",
                    "allowed_tools": ["read_file"], "max_turns": 4,
                })
            except ToolError:
                pass

    asyncio.run(go())
    end = _end(config)
    assert end["ended"] == "deadline", end
    assert end["turns"] == 1, end
