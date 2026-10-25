"""The `start` event names the workspace the session runs in.

The server narrows the configured roots to the session's project (ADR-0110), and the
project is now `Config.project_dir` -- the folder `CLAUDE_PROJECT_DIR` names -- not a root
the client lists (ADR-0128). Which folder the delegation actually ran under is a separate
fact, and the stream's `start` event carries its *name* -- the last path component -- so a
reader can tell one project from another in a directory of transcripts.

The name is the session's own project, not some other root in the configured ceiling:
`paths.narrow_roots` returns the narrowed roots in the *ceiling's* order, so code that
takes the result of that would name a different project whenever the session's project is
not the ceiling's first root.
"""

import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client

from claude_delegate_local import server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry


def start_for(config) -> dict:
    """Run one read-only delegation and return its stream's `start` event.

    The client lists no roots: the server no longer reads them, so the session's project
    comes entirely from `Config.project_dir`.
    """
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, recording_handler([])))
    args = {"task": "read it", "effort": "inherit"}

    async def go():
        async with Client(mcp) as client:
            await answered(client, "delegate_readonly", args)

    asyncio.run(go())
    streams = list(Path(config.transcript_dir).glob("*.jsonl"))
    assert len(streams) == 1, [p.name for p in streams]
    events = [json.loads(ln) for ln in streams[0].read_text(encoding="utf-8").splitlines()
              if ln]
    start = next(e for e in events if e["t"] == "start")
    assert start["t"] == "start"
    return start


def test_the_project_directory_names_the_workspace(tmp_path):
    """The session's project -- `Config.project_dir` -- names the workspace it ran under."""
    proj = tmp_path / "proj"
    proj.mkdir()
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"),
                       project_dir=os.path.realpath(proj))
    start = start_for(config)
    assert start["workspace"] == "proj"


def test_the_name_is_the_project_not_the_first_ceiling_root(tmp_path):
    """ORDER GUARD. The ceiling is two roots X then Y; the session's project is Y.

    `paths.narrow_roots` returns the narrowed roots in the *ceiling's* order, so code that
    takes its first element would name X. The name must follow the session's project, so it
    is Y.
    """
    x, y = tmp_path / "X", tmp_path / "Y"
    x.mkdir()
    y.mkdir()
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"),
                       workspace_roots=(os.path.realpath(x), os.path.realpath(y)),
                       project_dir=os.path.realpath(y))
    start = start_for(config)
    assert start["workspace"] == "Y"


def test_a_server_without_a_project_omits_the_workspace(tmp_path):
    """`narrow` fallback: the session did not say where it works, so the key is absent,
    not written as null."""
    config = files_cfg(tmp_path, transcript_dir=str(tmp_path / "tr"))
    start = start_for(config)
    assert "workspace" not in start
