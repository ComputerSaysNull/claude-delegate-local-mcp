"""A batch worked in silence, so the caller gave up on work the cluster kept doing.

`delegate_batch` passed `ctx=None` into every item on purpose: turn counts interleaved
from items running at once describe nothing a reader could act on, so progress was
reported per finished item instead. The reasoning is right about what a reader sees and
wrong about what the notification is for. ADR-0018 exists to reset the client's stdio
idle timer, and `run_delegation`'s own docstring says resetting that timer is the whole
of its job -- so reporting only on completion left a batch whose items each run longer
than the 1800s timeout sending nothing at all.

Observed: a two-item batch aborted by the client at 1800s while both items were still
generating. The server does not learn that the caller has gone, so it carried on for the
rest of `dispatch_timeout` -- 3616 seconds from admission to release, measured -- holding
`max_inflight_large_prefills` at its ceiling of 2 the entire time. Every other session on
the machine was locked out of large delegations for half an hour after the caller had
already been told the call failed.

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
from test_server import DoubleCache, cfg, chat_reply, entry, registry, tool_call_reply

TASKS = ["summarise the first thing", "summarise the second thing"]


def two_turns_each(request: httpx.Request) -> httpx.Response:
    """Give every item exactly two turns, whatever order the concurrent items arrive in.

    A scripted list of replies cannot do this: `turn_handler` pops from one shared
    sequence, so two items running at once take each other's turns and the count under
    test becomes a function of interleaving. Reading the conversation instead makes each
    reply depend only on the request that asked for it -- a request carrying a tool
    result is on its second turn and is answered; one that is not is asked for a tool.
    """
    body = json.loads(request.content)
    answered = any(m.get("role") == "tool" for m in body.get("messages", []))
    if answered:
        return httpx.Response(200, json=chat_reply(content="done"))
    return httpx.Response(
        200, json=tool_call_reply("read_file", {"path": "/nowhere/at/all.py"})
    )


def run_batch() -> tuple[dict, list[tuple[float, float | None]]]:
    """Run a two-item batch and collect every progress notification the client saw."""
    config = cfg(max_turns_default=4)
    mcp = server.build(
        config, registry(entry()), DoubleCache(config, two_turns_each)
    )
    seen: list[tuple[float, float | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total))

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            return (await client.call_tool("delegate_batch", {"tasks": TASKS, "effort": "inherit"})).data

    return asyncio.run(go()), seen


def test_a_batch_notifies_while_it_works_not_only_when_an_item_lands():
    """The bug itself: notifications must outnumber the items, or the timer is not reset.

    Two items of two turns each is four turns of work. Reporting only on completion sends
    two notifications for it, both arriving after the turn that would have timed out.
    """
    result, seen = run_batch()

    assert result["count"] == len(TASKS)
    assert result["failed"] == [], f"the batch itself must still succeed: {result}"
    assert len(seen) > len(TASKS), (
        "a batch reporting only on completion sends one notification per item and "
        f"nothing during the turns that take the time; saw {len(seen)} for "
        f"{len(TASKS)} items: {seen}"
    )


def test_a_batch_never_reports_a_turn_number_to_the_client():
    """The constraint the original `ctx=None` was protecting, kept intact.

    Interleaved turn counts were the reason progress was withheld, so restoring the
    notification must not restore them: every number the client sees is the batch's own
    completed count out of the items asked for.
    """
    _, seen = run_batch()

    assert seen, "no notifications at all"
    for progress, total in seen:
        assert total == len(TASKS), f"total must be the item count, saw {total}"
        assert 0 <= progress <= len(TASKS), (
            f"progress must be a completed-item count, saw {progress} of {total}"
        )
