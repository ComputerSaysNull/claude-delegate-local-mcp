"""The viewer printed a turn's heartbeats above the turn they belonged to.

A turn's calls reach the stream only in its `turn` event, written once every call has
finished, so the heartbeats that beat while they ran came first and read as belonging to
the turn before. The calls are now announced in a `tools` event as they start, each
heartbeat says which are queued, running or done, and the viewer opens a turn at its budget
line and closes it after its results.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from test_agentic_loop import ScriptedTurns, cfg, entry, says, wants

import scripts.watch_delegations as wd
from claude_delegate_local import loop, tools, transcript
from claude_delegate_local.backends.base import ToolSpec


def _register(name: str, seconds: float) -> None:
    def handler(cfg_, args):
        time.sleep(seconds)  # real time, so the heartbeat's timer fires during it
        return "ok"

    tools.REGISTRY[name] = tools.RegisteredTool(
        spec=ToolSpec(name=name, description=name, input_schema={"type": "object"}),
        handler=handler,
        cacheable=False,  # each its own group, so they run one after the other
    )


def test_the_calls_are_announced_and_each_beat_says_where_each_one_is():
    events: list[tuple] = []

    async def on_alive(elapsed, of, ends_in, chunks=0, reasoning_chunks=0,  # noqa: PLR0913, PLR0917 -- the heartbeat's positional arity, fixed by _keepalive
                       since=None, running=()):
        if running:
            events.append(("alive", tuple((c["name"], c["status"]) for c in running)))

    async def on_tools(row):
        events.append(("tools", tuple(c["name"] for c in row["tool_calls"]), row["turn"]))

    _register("first", 1.6)
    _register("second", 1.6)
    try:
        asyncio.run(loop.run_agentic_loop(
            cfg(keepalive_interval=1),
            entry(),
            ScriptedTurns(wants(("first", {}), ("second", {})), says("done")),
            loop.Delegation("do the thing"),
            allowed=frozenset({"first", "second"}),
            max_turns=5,
            on_alive=on_alive,
            on_tools=on_tools,
        ))
    finally:
        tools.REGISTRY.pop("first", None)
        tools.REGISTRY.pop("second", None)

    assert events[0] == ("tools", ("first", "second"), 1), events
    beats = [e[1] for e in events if e[0] == "alive"]
    assert (("first", "running"), ("second", "queued")) in beats, beats
    assert (("first", "done"), ("second", "running")) in beats, beats


def test_a_turn_says_whether_its_calls_were_announced(tmp_path):
    stream = transcript.open_stream(cfg(transcript_dir=str(tmp_path)), None)
    assert stream is not None
    stream.tools(turn=1, of_turns=5, tool_calls=[{"name": "read_file", "arguments": {}}])
    stream.turn(SimpleNamespace(turn=1, tool_calls=()), "")
    stream.turn(SimpleNamespace(turn=2, tool_calls=()), "")  # control: never announced

    (path,) = tmp_path.glob("*.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["t"] == "tools" and rows[0]["tool_calls"][0]["name"] == "read_file"
    turns = [r for r in rows if r["t"] == "turn"]
    assert [t["announced"] for t in turns] == [True, False]


def _screen(event: dict) -> str:
    return "\n".join(wd._plain(line) for line in wd.render(event, 100))


def test_the_viewer_opens_a_turn_lists_its_calls_and_closes_it():
    priced = _screen({"t": "priced", "turn": 2, "of_turns": 5, "budget_ceiling": 3200,
                      "decode_rate": 30.0})
    announced = _screen({"t": "tools", "turn": 2, "tool_calls": [
        {"name": "read_file", "arguments": {"path": "/w/a.py", "start_line": 3}},
        {"name": "run_bash", "arguments": {"command": "pytest -q"}},
    ]})
    done = _screen({"t": "turn", "turn": 2, "of_turns": 5, "announced": True, "tool_calls": [
        {"name": "read_file", "outcome": "ran", "arguments": {"path": "/w/a.py"},
         "result_bytes": 640, "result_lines": 12, "ms": 30},
        {"name": "run_bash", "outcome": "error", "arguments": {"command": "pytest -q"},
         "message": "exit 1"},
    ]})

    assert priced.splitlines()[1].startswith("┄") and "turn 2 of 5" in priced
    assert "▸ read_file" in announced and "/w/a.py" in announced and "ran" not in announced
    assert "pytest -q" in announced
    assert "turn 2 of 5 done" in done
    assert "▸ read_file ran" in done and "12 lines" in done
    assert "▸ run_bash error" in done and "exit 1" in done
    assert "/w/a.py" not in done, "an announced call's arguments were printed twice"
    assert done.splitlines()[-1].startswith("┄"), "the turn has no closing rule"


def test_a_turn_from_before_the_announcement_still_shows_its_arguments():
    """Control: an older stream has no `tools` event, so its turn must show everything."""
    old = _screen({"t": "turn", "turn": 2, "tool_calls": [
        {"name": "read_file", "outcome": "ran", "arguments": {"path": "/w/a.py"}},
    ]})
    assert "/w/a.py" in old and "done" not in old.splitlines()[1]


def test_the_heartbeat_colours_each_call_by_where_it_is():
    line = wd._plain(wd._alive_line({
        "elapsed_seconds": 41.0, "of_seconds": 3600, "ends_in_seconds": 120.0,
        "chunks_seen": 0, "running": [
            {"name": "read_file", "arguments": {}, "status": "done"},
            {"name": "run_bash", "arguments": {}, "status": "running"},
        ],
    }))
    assert "tools: read_file done, run_bash running" in line, line
