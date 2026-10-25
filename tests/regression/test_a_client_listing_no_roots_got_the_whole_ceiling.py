"""A session that has not said where it works got the whole ceiling.

With `client_roots` defaulting to `narrow`, a server started without a project directory
(`project_dir=None`) was handed `DELEGATE_WORKSPACE_ROOTS` unchanged. Since the roots
became a ceiling, the setup meant is one wide parent folder, so that was every project
under it. The default is now `require`, which refuses a session that has not said where it
works instead (ADR-0119). The session's project comes from `CLAUDE_PROJECT_DIR`, so the
refusal names it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastmcp import Client

from claude_delegate_local import config, server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry


def call(cfg) -> dict:
    """One read-only delegation over a real MCP session for `cfg`'s project."""
    mcp = server.build(cfg, registry(entry()), DoubleCache(cfg, recording_handler([])))

    async def go():
        async with Client(mcp) as client:
            return await answered(client, "delegate_readonly",
                                  {"task": "t", "effort": "inherit"})

    return asyncio.run(go())


DEFAULT = config.Config.__dataclass_fields__["client_roots"].default


def test_the_default_is_require():
    assert DEFAULT == "require"


def test_a_session_without_a_project_is_refused_by_default(tmp_path):
    cfg = replace(files_cfg(tmp_path), client_roots=DEFAULT, project_dir=None)
    with pytest.raises(Exception, match="CLAUDE_PROJECT_DIR"):
        call(cfg)


def test_narrow_still_hands_the_configured_roots_to_such_a_session(tmp_path):
    """The negative control: the refusal comes from the default, not from the session."""
    call(replace(files_cfg(tmp_path), client_roots="narrow", project_dir=None))
