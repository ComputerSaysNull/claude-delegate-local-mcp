"""A client listing no roots got the whole ceiling.

With `client_roots` defaulting to `narrow`, a client that lists no folders was handed
`DELEGATE_WORKSPACE_ROOTS` unchanged. Since the roots became a ceiling, the setup meant is
one wide parent folder, so that was every project under it. The default is now `require`,
which refuses such a client instead (ADR-0119).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastmcp import Client

from claude_delegate_local import config, server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry


def call(cfg) -> dict:
    """One read-only delegation over a real MCP session whose client lists no roots."""
    mcp = server.build(cfg, registry(entry()), DoubleCache(cfg, recording_handler([])))

    async def go():
        async with Client(mcp) as client:
            return await answered(client, "delegate_readonly",
                                  {"task": "t", "effort": "inherit"})

    return asyncio.run(go())


DEFAULT = config.Config.__dataclass_fields__["client_roots"].default


def test_the_default_is_require():
    assert DEFAULT == "require"


def test_a_client_listing_no_roots_is_refused_by_default(tmp_path):
    cfg = replace(files_cfg(tmp_path), client_roots=DEFAULT)
    with pytest.raises(Exception, match="lists no MCP roots"):  # fastmcp's own error type
        call(cfg)


def test_narrow_still_hands_the_configured_roots_to_such_a_client(tmp_path):
    """The negative control: the refusal comes from the default, not from the client."""
    call(replace(files_cfg(tmp_path), client_roots="narrow"))
