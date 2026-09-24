"""The priced event recorded what the caller allowed, not what the model was sent.

`on_priced` carried `"max_tokens": max_tokens`, which is the caller's argument and is
`None` whenever the caller passed none. The budget actually sent is what
`resolve_max_tokens` produces, later, in `dispatch_with_recovery` -- so the one event a
reader uses to know what a turn was allowed could say "no budget" about a turn that was
sent a real one, and the two numbers disagreed whenever the caller relied on the
configured default. The record that existed to explain a turn could not say what it was
given.

`max_tokens` stays, so existing readers keep working; `max_tokens_sent` is added beside it,
carrying the resolved budget of the first attempt. Recovery stages that enlarge the budget
on a retry happen *after* pricing, so `max_tokens_sent` is the first attempt's budget, and
`max_tokens` remains the caller's raw argument.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastmcp import Client

from claude_delegate_local import loop, server
from wire_double import as_stream
from test_server import DoubleCache, answered, cfg, chat_reply, entry, registry
from test_transcript_stream import _events, two_turns


def _recording(handler, sent: list) -> object:
    """Wrap a chat handler, recording each chat request and answering the metrics scrape.

    The scrape is a GET to `/metrics`; answering it 404 keeps `probe_cluster`'s "no
    metrics surface" answer, so the pricing ceiling comes from the configured fallback
    rather than from a planted reading. Only POSTs carry a chat body worth recording.
    """

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(404)
        sent.append(json.loads(request.content))
        return handler(request)

    return wrapped


def _drive(tmp_path: Path, handler, tool: str, args: dict, **over):
    """One delegation over a real MCP session, returning what the test needs to check it.

    The transcript stream and the recorded request bodies are both returned, because the
    assertion is that the priced row and the wire agree -- and neither is derivable from
    the other without running the delegation.
    """
    config = cfg(transcript_dir=str(tmp_path), max_turns_default=4, **over)
    the_entry = entry()
    sent: list = []
    mcp = server.build(
        config, registry(the_entry), DoubleCache(config, _recording(handler, sent))
    )
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            return await answered(client, tool, args)

    asyncio.run(go())
    return config, the_entry, sent, _events(tmp_path)


def _priced(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("t") == "priced"]


def _sent_budget(config, entry, row: dict) -> int:
    """What `resolve_max_tokens` returns for the configuration the row describes.

    Read from the row itself rather than assumed: the effort and the ceiling are resolved
    inside the loop, and re-deriving them here would be the second computation the record
    exists to prevent disagreeing with.
    """
    return loop.resolve_max_tokens(
        config, entry, row["effort"], None, ceiling=row["budget_ceiling"]
    )


def test_a_one_shot_priced_row_names_the_budget_actually_sent(tmp_path: Path):
    """The one-shot path: the caller passes no budget, and the record must still say what
    was sent."""
    config, the_entry, sent, events = _drive(
        tmp_path,
        lambda r: as_stream(chat_reply(content="a one-shot answer")),
        "delegate", {"task": "summarise", "allowed_tools": []},
    )
    rows = _priced(events)
    assert rows, f"no priced event in {[e['t'] for e in events]}"
    row = rows[0]

    assert row["max_tokens"] is None, row
    expected = _sent_budget(config, the_entry, row)
    assert row["max_tokens_sent"] == expected, row
    assert sent, "no request reached the backend"
    assert sent[0]["max_tokens"] == expected, sent[0]


def test_an_agentic_priced_row_names_the_budget_actually_sent(tmp_path: Path):
    """The turn loop: every turn is priced, and each priced row must name the budget that
    turn's first attempt was actually sent."""
    config, the_entry, sent, events = _drive(
        tmp_path, two_turns, "delegate", {"task": "explain the retry"},
    )
    rows = _priced(events)
    assert rows, f"no priced event in {[e['t'] for e in events]}"
    assert len(sent) >= len(rows), (
        f"{len(sent)} chat request(s) for {len(rows)} priced row(s)"
    )

    for row, body in zip(rows, sent, strict=True):
        assert row["max_tokens"] is None, row
        expected = _sent_budget(config, the_entry, row)
        assert row["max_tokens_sent"] == expected, row
        assert body["max_tokens"] == expected, body
