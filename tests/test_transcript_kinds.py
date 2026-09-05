"""Which call a transcript came from, and what that call was handed.

Split from `test_transcript_stream.py`, which covers the order and timing of the events.
These cover their contents: the tool that was invoked, the tools it resolved to, and the
files it was given. Until those were recorded a `delegate_readonly` call and a plain
`delegate` wrote byte-identical transcripts, so a directory of them could not be counted
by kind and a reader could not see what a delegation was actually working on.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client

from claude_delegate_local import server
from test_server import (
    DoubleCache,
    cfg,
    chat_handler,
    files_cfg,
    files_posix_only,
    entry,
    registry,
)


def _call(config, handler, tool: str, args: dict) -> None:
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))

    # `effort` is required on every delegation tool, and "inherit" is the value that
    # preserves the pre-ADR-0045 behaviour exactly: it falls through to the agent, the
    # registry row and then `thinking_default`. A test that cares about the level sets
    # it itself; that it is *refused* when absent is asserted directly, not here.
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            await client.call_tool(tool, args)

    asyncio.run(go())


def _starts(directory: Path) -> list[dict]:
    """The start event of every stream in the directory, oldest name first."""
    out = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("t") == "start":
                out.append(event)
                break
    return out


def _record(directory: Path) -> dict:
    records = sorted(directory.glob("*.json"))
    assert len(records) == 1, [p.name for p in records]
    return json.loads(records[0].read_text(encoding="utf-8"))


def test_a_read_only_delegation_says_so_rather_than_calling_itself_delegate(tmp_path):
    """The pair that motivated this. `delegate_readonly` is `delegate` with the toolset
    fixed, so it runs the same path and nothing about the shape names it -- the record has
    to carry which tool the caller actually invoked.

    The toolset is no longer empty (ADR-0048), which makes the point sharper rather than
    weaker: `tools` alone cannot distinguish this from a `delegate` call that happened to
    name the same two, so `tool` is the only field that says what was invoked.
    """
    config = cfg(transcript_dir=str(tmp_path))
    _call(config, chat_handler(), "delegate_readonly", {"task": "explain this"})

    start = _starts(tmp_path)[0]
    assert start["tool"] == "delegate_readonly", start
    assert start["tools"] == ["read_file", "read_git", "search_files"], start
    assert _record(tmp_path)["tool"] == "delegate_readonly"


def test_a_plain_delegation_records_the_tools_it_was_given(tmp_path):
    """The other side of the same field: `delegate` alone does not say whether a loop
    ran, and a delegation handed no tools is a one-shot whatever it was called."""
    config = cfg(transcript_dir=str(tmp_path))
    _call(config, chat_handler(), "delegate", {"task": "explain this"})

    start = _starts(tmp_path)[0]
    assert start["tool"] == "delegate"
    assert start["tools"], "a plain delegate resolves to whatever the host can run"
    assert "read_file" in start["tools"], start["tools"]



@files_posix_only
def test_a_stream_says_which_files_the_delegation_was_given(tmp_path):
    """The record carries this already, and the record is written when the work is over.
    A reader asking what a delegation is chewing on is asking while it runs."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    good = workspace / "a.py"
    good.write_text("x = 1\n", encoding="utf-8")
    blob = workspace / "b.py"
    blob.write_bytes(b"\x00\x01binary")
    out = tmp_path / "out"

    config = files_cfg(workspace, transcript_dir=str(out))
    _call(config, chat_handler(), "delegate",
          {"task": "review this", "files": [str(good), str(blob)]})

    start = _starts(out)[0]
    assert [f["path"] for f in start["files_read"]] == [os.path.realpath(good)]
    assert start["files_skipped"][0]["kind"] == "binary"
    assert start["prefetch_tokens"] > 0, start
