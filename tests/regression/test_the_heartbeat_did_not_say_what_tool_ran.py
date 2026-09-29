"""While a turn's tools ran, the heartbeat said nothing about them.

The chunk count froze and the countdown fell, which reads exactly like a delegation gone
quiet, and nothing named the command that was running: a turn's tool calls are written only
once they finish. The heartbeat now carries the running batch, each call's name and its
arguments capped as the turn record caps them, and the viewer prints it.
"""

from __future__ import annotations

import asyncio
import json
import time

from test_agentic_loop import ScriptedTurns, cfg, entry, says, wants

import scripts.watch_delegations as wd
from claude_delegate_local import loop, tools, transcript
from claude_delegate_local.backends.base import ToolSpec


def _slow_tool(seconds: float):
    def slow(cfg_, args):
        time.sleep(seconds)  # real time, so the heartbeat's real-clock timer fires during it
        return "took a while"

    tools.REGISTRY["slow"] = tools.RegisteredTool(
        spec=ToolSpec(name="slow", description="slow", input_schema={"type": "object"}),
        handler=slow,
        cacheable=False,
    )


def test_a_beat_during_a_tool_names_the_tool_and_its_arguments():
    beats: list[tuple] = []

    async def on_alive(elapsed, of, ends_in, chunks=0, reasoning_chunks=0,  # noqa: PLR0913, PLR0917 -- the heartbeat's positional arity, fixed by _keepalive
                       since=None, running=()):
        beats.append(running)

    _slow_tool(2.5)
    try:
        asyncio.run(loop.run_agentic_loop(
            cfg(keepalive_interval=1),
            entry(),
            ScriptedTurns(wants(("slow", {"command": "pytest -q tests"})), says("done")),
            loop.Delegation("do the thing"),
            allowed=frozenset({"slow"}),
            max_turns=5,
            on_alive=on_alive,
        ))
    finally:
        tools.REGISTRY.pop("slow", None)

    named = [r for r in beats if r]
    assert named, f"no beat during the tool named anything: {beats}"
    assert named[0] == ({"name": "slow", "arguments": {"command": "pytest -q tests"}},)


def test_a_one_shot_beat_names_no_tool():
    """Control: the one-shot has no tools, so its beat carries an empty batch."""
    beats: list[tuple] = []

    async def on_alive(elapsed, of, ends_in, chunks=0, reasoning_chunks=0,  # noqa: PLR0913, PLR0917 -- the heartbeat's positional arity, fixed by _keepalive
                       since=None, running=("never called with this",)):
        beats.append(running)

    async def go():
        task = asyncio.create_task(loop._keepalive(
            cfg(keepalive_interval=1), on_alive, lambda: 0.0, lambda: 10.0,
        ))
        await asyncio.sleep(1.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert beats == [()], beats


def test_the_transcript_records_the_batch_only_while_one_runs(tmp_path):
    stream = transcript.open_stream(cfg(transcript_dir=str(tmp_path)), None)
    assert stream is not None
    running = ({"name": "run_bash", "arguments": {"command": "pytest -q"}},)
    stream.alive(elapsed_seconds=1.0, of_seconds=60, running=running)
    stream.alive(elapsed_seconds=2.0, of_seconds=60)

    (path,) = tmp_path.glob("*.jsonl")
    beats = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if json.loads(line)["t"] == "alive"]
    assert beats[0]["running"] == [{"name": "run_bash", "arguments": {"command": "pytest -q"}}]
    assert "running" not in beats[1], "a beat with nothing running changed shape"


def test_the_viewer_says_what_is_running():
    event = {
        "t": "alive", "elapsed_seconds": 41.0, "of_seconds": 3600, "ends_in_seconds": 120.0,
        "chunks_seen": 900, "since_chunk_seconds": 30.0,
        "running": [{"name": "run_bash", "arguments": {"command": "pytest -q tests"}}],
    }
    line = wd._alive_line(event)
    assert "running run_bash: pytest -q tests" in line, line
    del event["running"]
    assert "running run_bash" not in wd._alive_line(event)
