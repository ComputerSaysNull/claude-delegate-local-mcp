"""Upstream's first transcript bug: failure paths did not carry the agent name.

So the dispatches the transcript existed to explain -- the ones that went wrong -- were
exactly the ones logged as `unknown`, while every successful record named its agent
correctly. A log that is accurate about the uninteresting case and blank about the
interesting one is worse than none, because it is trusted.

The cause is structural rather than a slip. The agent's *name* is in scope only at the top
of `run_delegation`; anything further in has a `Delegation`, which carries the task and the
agent body but not the name. Assemble the record down there and a failure has nothing to
name. So the identity is captured before the attempt and written from a `finally`.

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
from test_server import DoubleCache, cfg, entry, registry

AGENT = """---
name: reviewer
description: reviews things
---
You review things.
"""


def with_agent(tmp_path: Path, handler, **over) -> tuple[dict | None, list[dict]]:
    """Run one `delegate_to_agent` against a real agent file, and read what was written."""
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "reviewer.md").write_text(AGENT, encoding="utf-8")
    out = tmp_path / "transcripts"

    config = cfg(agents_dir=str(agents), transcript_dir=str(out), **over)
    entries = (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    async def go():
        async with Client(mcp) as client:
            return (
                await client.call_tool(
                    "delegate_to_agent", {"agent_name": "reviewer", "task": "look at this"}
                )
            ).data

    try:
        result = asyncio.run(go())
    except Exception:
        result = None

    written = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("*.json"))
    ]
    return result, written


def failing_handler(request):
    return httpx.Response(500, text="the endpoint fell over")


def ok_handler(request):
    return httpx.Response(
        200,
        json={
            "model": "served-id-1",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


def test_a_failed_dispatch_is_recorded_at_all(tmp_path):
    """First, that a failure is recorded. Nothing else here matters if it is not."""
    result, written = with_agent(tmp_path, failing_handler)
    assert result is None, "the dispatch was supposed to fail"
    assert len(written) == 1, "a failed dispatch wrote no transcript"


def test_a_failed_dispatch_still_names_its_agent(tmp_path):
    """The bug itself."""
    _, written = with_agent(tmp_path, failing_handler)
    assert written[0]["agent"] == "reviewer", written[0]


def test_a_failed_record_names_the_agent_in_its_filename_too(tmp_path):
    """The filename is how an operator finds the record without opening every file.

    Losing the name here restores the bug in the only form that still matters once the
    record itself is correct: a directory of files nobody can search.
    """
    out = tmp_path / "transcripts"
    with_agent(tmp_path, failing_handler)
    assert any("reviewer" in p.name for p in out.glob("*.json"))


def test_the_failure_record_says_what_went_wrong(tmp_path):
    """A record of a failure that does not say it failed explains nothing."""
    _, written = with_agent(tmp_path, failing_handler)
    one = written[0]
    assert one["ok"] is False
    assert one["error"], "no error recorded on a failed dispatch"


def test_success_and_failure_records_name_the_agent_the_same_way(tmp_path):
    """The asymmetry *is* the bug, so it is asserted directly rather than implied.

    Upstream's success path was correct throughout; only the comparison reveals that the
    other one was not.
    """
    _, failed = with_agent(tmp_path / "a", failing_handler)
    _, succeeded = with_agent(tmp_path / "b", ok_handler)
    assert failed[0]["agent"] == succeeded[0]["agent"] == "reviewer"
