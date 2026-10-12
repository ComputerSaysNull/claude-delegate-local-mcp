"""A prefetched line range recorded only the cost, so two ranges of one file read as two
identical entries (contract #69).

When `files[]` names a line range, the transcript's `start` event `files_read` entry holds
only path, given, bytes and est_tokens -- not which lines were read. Two separate ranges of
one file therefore produce two indistinguishable entries, and a reader (or the caller's
accounting) cannot tell what part of the file the delegation was actually given. Wanted: a
ranged entry carries `start_line` and `end_line` (1-based, inclusive, the lines actually
read, `end_line` clamped to the file's last line), while a whole-file entry carries neither
key.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client

from claude_delegate_local import context, server
from claude_delegate_local.config import Config
from claude_delegate_local.paths import ResolvedPath
from test_server import (
    answered,
    DoubleCache,
    chat_handler,
    entry,
    files_cfg,
    files_posix_only,
    registry,
)

UNPROVEN = (
    "PREFETCH UNPROVEN BY THIS RUN -- this is not a pass. It reads real files through "
    "resolved POSIX paths, and the server runs in WSL. Run it there -- see "
    "CONTRIBUTING.md: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest tests/regression/"
    "test_a_prefetched_range_did_not_say_which_lines.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def write(tmp_path, name: str, data: bytes | str) -> ResolvedPath:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return ResolvedPath(given=str(p), posix=os.path.realpath(p))


def lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


# ---- the accounting says which lines, not merely what it cost ---------------------------


@posix_only
def test_two_ranges_of_one_file_say_which_lines_each_was(tmp_path):
    """Two ranges must not come back as two identical entries (contract #69).

    Today each `files_read` row carries only path, given, bytes and est_tokens, so a
    reader cannot tell the 10-20 block from the 50-60 one -- which is the whole bug.
    """
    item = write(tmp_path, "a.py", lines(100))
    result = context.prefetch(
        cfg(),
        (
            context.FileRequest(entry=item, start_line=10, end_line=20),
            context.FileRequest(entry=item, start_line=50, end_line=60),
        ),
    )

    read = result.accounting()["files_read"]
    assert len(read) == 2, f"read: {read}"
    assert (read[0]["start_line"], read[0]["end_line"]) == (10, 20)
    assert (read[1]["start_line"], read[1]["end_line"]) == (50, 60)


@posix_only
def test_a_range_past_the_end_records_the_last_line_read(tmp_path):
    """`end_line` past the end means the end, so the recorded span is the one actually read."""
    item = write(tmp_path, "a.py", lines(30))
    result = context.prefetch(
        cfg(), (context.FileRequest(entry=item, start_line=25, end_line=999),)
    )

    row = result.accounting()["files_read"][0]
    assert (row["start_line"], row["end_line"]) == (25, 30)


@posix_only
def test_a_range_ending_on_a_blank_line_counts_that_line(tmp_path):
    """A blank line is still a line, and an inclusive end counts it."""
    item = write(tmp_path, "a.py", "a\nb\n\nc\n")
    result = context.prefetch(
        cfg(), (context.FileRequest(entry=item, start_line=2, end_line=3),)
    )

    row = result.accounting()["files_read"][0]
    assert (row["start_line"], row["end_line"]) == (2, 3)


@posix_only
def test_a_whole_file_has_no_line_range(tmp_path):
    """A whole-file entry keeps the old shape: no range keys at all, so a reader can tell a
    whole file from a range that happens to cover it."""
    item = write(tmp_path, "a.py", "x = 1\n")
    row = context.prefetch(cfg(), (item,)).accounting()["files_read"][0]

    assert "start_line" not in row
    assert "end_line" not in row


# ---- the transcript's start event carries the range (contract #69) ----------------------


def _call(config, handler, tool: str, args: dict) -> None:
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            await answered(client, tool, args)

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


@files_posix_only
def test_the_start_event_carries_the_range(tmp_path):
    """The transcript's `start` event is what an operator sees while a delegation runs.

    `accounting()` is shared with the `start` event via `_files`, so the fix must reach
    there too -- otherwise the reader the contract is for stays blind.
    """
    workspace = tmp_path / "work"
    workspace.mkdir()
    target = workspace / "a.py"
    target.write_text(lines(5), encoding="utf-8")
    out = tmp_path / "out"

    config = files_cfg(workspace, transcript_dir=str(out))
    _call(
        config, chat_handler(), "delegate",
        {
            "task": "review this",
            "files": [{"path": str(target), "start_line": 2, "end_line": 4}],
        },
    )

    start = _starts(out)[0]
    row = start["files_read"][0]
    assert row["start_line"] == 2
    assert row["end_line"] == 4
