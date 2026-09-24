"""The tools' destructive and open-world hints were left to the specification's defaults.

Unset, both default to true, so the four read-only tools declared themselves read-only and,
by omission, destructive and open to the world; and no tool had a `title`. Claude Code acts
on `readOnlyHint` alone, so nothing here broke, but a client that shows or gates on the
other two was told something untrue about every tool.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

from test_server import build_default

READ_ONLY = ("backend_status", "list_agents", "collect", "delegate_readonly",
             "delegate_to_agent_readonly")
WRITING = ("delegate", "delegate_to_agent")
# The sandbox has no network unless an agent asks and is on `agent_network_allowed`, and
# only `delegate_to_agent` runs an agent that can ask.
OPEN_WORLD = {"delegate_to_agent"}


def _tools() -> dict:
    async def go():
        async with Client(build_default()) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(go())


def test_every_tool_has_a_title() -> None:
    tools = _tools()
    for name, tool in tools.items():
        assert tool.title, f"{name} has no title"


def test_the_destructive_and_open_world_hints_are_stated_and_true() -> None:
    tools = _tools()
    for name in READ_ONLY + WRITING:
        a = tools[name].annotations
        assert a is not None, name
        assert a.destructiveHint is (name in WRITING), f"{name}: {a.destructiveHint}"
        assert a.openWorldHint is (name in OPEN_WORLD), f"{name}: openWorldHint {a.openWorldHint}"
