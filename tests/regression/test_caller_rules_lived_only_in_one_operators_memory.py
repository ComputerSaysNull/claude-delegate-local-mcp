"""Two rules for shaping a delegation lived only in one operator's notes (PLAN M15.7).

Give a verifying pass the reading tools and check its quotations yourself, and bound a pass
with `max_turns` rather than with prose. Anyone else given this server met neither. Their
home is `delegate://orchestration`, the long form the model reads on demand. A third --
write-capable calls released one per 120s -- is not here on purpose: ADR-0103 removed it.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

from test_server import build_default


def _resource() -> str:
    async def go():
        async with Client(build_default()) as client:
            return (await client.read_resource("delegate://orchestration"))[0].text

    return asyncio.run(go())


def test_the_orchestration_resource_carries_both_caller_rules() -> None:
    text = " ".join(_resource().split())
    assert "not to verify its own quotations" in text
    assert "Bound a pass with `max_turns`, not with prose" in text
    assert "cannot stop a loop inside one turn" in text


def test_it_no_longer_tells_a_caller_to_budget_for_a_stagger() -> None:
    text = " ".join(_resource().split())
    assert "answer at once with `status: running`" in text
    assert "one per 120s" not in text
