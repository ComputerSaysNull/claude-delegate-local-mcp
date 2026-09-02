"""Upstream's second transcript bug: setting the log directory changed the response.

The success path merged the transcript's whole payload into ordinary responses as soon as
the directory was configured, contradicting the design's own claim that it "does not change
the response shape". An operator turning on logging silently changed what every caller
received.

It was a dict-merge accident, and `run_delegation` still returns a dict assembled from
several conditional `**` spreads -- so the shape that caused it is still there, and a stray
`**record` in that expression would do it again. The defence is that the writer returns
nothing: it is called from a `finally` for its effect, and there is no value for the return
expression to pick up.

Asserted by equality of the whole response rather than by checking for particular keys. A
test that looked for known transcript field names would pass against a leak of a field
nobody thought to list, which is the leak that would actually happen.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_delegate_local import server
from test_server import DoubleCache, cfg, chat_reply, entry, registry


def handler(request):
    return httpx.Response(200, json=chat_reply(content="the answer"))


def response_with(config) -> dict:
    entries = (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    async def go():
        async with Client(mcp) as client:
            return (
                await client.call_tool("delegate", {"task": "a question", "effort": "inherit"})
            ).data

    return asyncio.run(go())


def test_the_transcript_never_reaches_the_response(tmp_path):
    """The whole response, byte for byte, with the directory set and unset."""
    off = response_with(cfg())
    on = response_with(cfg(transcript_dir=str(tmp_path)))

    assert json.dumps(off, sort_keys=True, default=str) == json.dumps(
        on, sort_keys=True, default=str
    )


def test_the_transcript_was_actually_written_during_that_comparison(tmp_path):
    """Without this the test above passes when the transcript does nothing at all.

    Two identical responses prove nothing if the feature was off in both runs -- which is
    exactly how a check that cannot fail gets written.
    """
    response_with(cfg(transcript_dir=str(tmp_path)))
    assert list(tmp_path.glob("*.json")), "nothing was written, so nothing was proven"


def test_diagnostics_stay_off_for_the_caller_when_a_transcript_is_on(tmp_path):
    """The transcript forces per-turn records on internally, because the loop only keeps
    them when told to. That must not reach the caller: `diagnostics` is the caller's
    request about the caller's reply, and an operator's logging setting is not an answer
    to it."""
    on = response_with(cfg(transcript_dir=str(tmp_path)))
    assert "diagnostics" not in on


def test_the_caller_can_still_ask_for_diagnostics_with_a_transcript_on(tmp_path):
    """The negative half: forcing the flag internally must not have disconnected it."""
    config = cfg(transcript_dir=str(tmp_path))
    entries = (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    async def go():
        async with Client(mcp) as client:
            return (
                await client.call_tool(
                    "delegate",
                    {
                        "task": "x",
                        "effort": "inherit",
                        "allowed_tools": ["read_file"],
                        "diagnostics": True,
                    },
                )
            ).data

    assert "diagnostics" in asyncio.run(go())
