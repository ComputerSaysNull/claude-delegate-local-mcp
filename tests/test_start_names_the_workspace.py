"""The `start` event names the workspace the session runs in.

The server already narrows the configured roots to the folders a client lists for its
session (ADR-0110). Which of those the delegation actually ran under is a separate fact,
and the stream's `start` event carries its *name* -- the last path component -- so a
reader can tell one project from another in a directory of transcripts.

The name comes from the folders the client lists, in the client's own order, not from the
configured ceiling's order: `paths.narrow_roots` re-sorts into the ceiling's order, so
taking the result of that would name a different project whenever the two orders differ.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client

from claude_delegate_local import server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="the path policy resolves POSIX paths; the server is POSIX-only")


def start_for(config, *, roots=None) -> dict:
    """Run one read-only delegation and return its stream's `start` event.

    `roots` is what the MCP client lists for the session; None lists none at all, which is
    the `narrow` fallback.
    """
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, recording_handler([])))
    args = {"task": "read it", "effort": "inherit"}

    async def go():
        kw = {} if roots is None else {"roots": [Path(r).as_uri() for r in roots]}
        async with Client(mcp, **kw) as client:
            await answered(client, "delegate_readonly", args)

    asyncio.run(go())
    streams = list(Path(config.transcript_dir).glob("*.jsonl"))
    assert len(streams) == 1, [p.name for p in streams]
    events = [json.loads(ln) for ln in streams[0].read_text(encoding="utf-8").splitlines()
              if ln]
    start = next(e for e in events if e["t"] == "start")
    assert start["t"] == "start"
    return start


@posix_only
def test_the_first_listed_folder_names_the_workspace(tmp_path):
    """Two folders inside the ceiling: the client's first one wins."""
    one, two = tmp_path / "one", tmp_path / "two"
    for p in (one, two):
        p.mkdir()
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"),
                       workspace_roots=(os.path.realpath(tmp_path),))
    start = start_for(config, roots=[one, two])
    assert start["workspace"] == "one"


@posix_only
def test_a_folder_outside_the_ceiling_first_does_not_name_it(tmp_path):
    """A client that lists something outside first is skipped, not a failure."""
    outside = tmp_path / "outside"
    outside.mkdir()
    inner = tmp_path / "proj" / "inner"
    inner.mkdir(parents=True)
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"),
                       workspace_roots=(os.path.realpath(tmp_path / "proj"),))
    start = start_for(config, roots=[outside, inner])
    assert start["workspace"] == "inner"


@posix_only
def test_the_client_order_names_the_workspace_not_the_ceiling_order(tmp_path):
    """ORDER PIN. The ceiling is two roots X then Y; the client lists Y first, X second.

    `paths.narrow_roots` returns the narrowed roots in the *ceiling's* order, so code that
    takes its first element would name X. The name must follow the client's own order, so
    it is Y.
    """
    x, y = tmp_path / "X", tmp_path / "Y"
    x.mkdir()
    y.mkdir()
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"),
                       workspace_roots=(os.path.realpath(x), os.path.realpath(y)))
    start = start_for(config, roots=[y, x])
    assert start["workspace"] == "Y"


@posix_only
def test_a_client_that_lists_no_roots_omits_the_workspace(tmp_path):
    """The `narrow` fallback: the session did not say where it works, so the key is absent,
    not written as null."""
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"))
    start = start_for(config, roots=None)
    assert "workspace" not in start
