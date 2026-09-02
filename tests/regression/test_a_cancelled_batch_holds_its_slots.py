"""A cancelled batch must not leave its items generating, or one abort locks the cluster.

The incident behind `#45` was a two-item `delegate_batch` that held
`max_inflight_large_prefills` at its ceiling for 3616 seconds -- past `dispatch_timeout` --
after the caller had already been told the call failed. An explicit abort was measured
against the live server on 2026-08-31 and found clean, but that was a single one-shot: the
incident's shape was a batch, whose items run concurrently under `asyncio.gather`, and
nothing tested whether the cancellation reaches them.

It does, and this pins it. Three separate things make it work -- `gather` is unshielded,
`run()` catches only `ToolError`, and `Admission.admit` releases in a `finally` -- and any
one of them could be changed by someone with a good local reason. Shielding the items would
look like a fix for interleaved failures and would silently restore the lockout.

Cancellation is sent the way a real client sends it, as `notifications/cancelled` for the
in-flight request. That detail is the test: the MCP SDK does **not** send one when the
caller's own task is cancelled or its read times out, so cancelling the client task instead
would leave the server working with both slots held, and every assertion here would still
pass -- satisfied during event-loop shutdown. Measured while writing this file.

What this cannot measure is a client that goes away without sending anything. That is the
stdio idle timeout, and no test reaches it.

Named after the bug it prevents, per the project's convention.
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
from test_server import DoubleCache, cfg, chat_reply, entry, models_reply, registry

TASKS = ["summarise the first thing", "summarise the second thing"]
NEVER = 600.0  # longer than the test can run; the point is that it is cut short
SETTLE = 40  # polls of 50ms: two seconds, against a live measurement of under one


class Blocked:
    """A backend that answers /v1/models at once and never answers a completion.

    Both halves matter. The completions have to still be in flight when the cancel lands,
    or this would assert about work that had already finished on its own; and
    `backend_status`, which is how the gauges are read afterwards, probes /v1/models and
    would otherwise block on the very stall it is being used to observe.
    """

    def __init__(self, items: int) -> None:
        self.all_started = asyncio.Event()
        self.started = 0
        self.cancelled = 0
        self._items = items

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=models_reply("served-id-1"))
        self.started += 1
        if self.started >= self._items:
            self.all_started.set()
        try:
            await asyncio.sleep(NEVER)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return httpx.Response(200, json=chat_reply(content="unreachable"))


def cancel_a_running_batch(tmp_path) -> dict:
    """Start a batch, wait until every item is really generating, then cancel it.

    Everything asserted on is snapshotted *inside* the running loop. Read afterwards it
    would include whatever `asyncio.run` cancels on its way out, which is how the first
    draft of this file passed against a cancellation that never reached the server at all.
    """
    records = tmp_path / "rec"
    config = cfg(slots_dir=str(tmp_path / "slots"), transcript_dir=str(records))
    blocked = Blocked(len(TASKS))
    mcp = server.build(config, registry(entry()), DoubleCache(config, blocked))

    async def go() -> dict:
        async with Client(mcp) as client:
            # The id `call_tool` is about to use. A client cancels a request by id, so the
            # test needs the same id; were it ever wrong the cancel would land nowhere and
            # every assertion below would fail rather than quietly pass.
            request_id = client.session._request_id
            call = asyncio.create_task(client.call_tool("delegate_batch", {"tasks": TASKS, "effort": "inherit"}))
            await asyncio.wait_for(blocked.all_started.wait(), timeout=10)

            await client.cancel(request_id)
            error: BaseException | None = None
            try:
                await call
            except BaseException as e:  # its type is the assertion
                error = e

            for _ in range(SETTLE):
                status = (await client.call_tool("backend_status", {})).data
                if status["admission"]["inflight_seqs"] == 0:
                    break
                await asyncio.sleep(0.05)

            return {
                "started": blocked.started,
                "cancelled": blocked.cancelled,
                "error": type(error).__name__ if error else None,
                "status": status,
                "records": [
                    json.loads(p.read_text(encoding="utf-8"))
                    for p in sorted(records.glob("*.json"))
                ],
            }

    return asyncio.run(go())


def test_a_cancelled_batch_cancels_every_item_it_gathered(tmp_path):
    """The mechanism: the cancel has to reach the items, not just the call that gathered.

    Asserting only that the gauges came back to zero would pass just as well if the items
    had never started, so the count that actually saw `CancelledError` is the assertion and
    `all_started` is what makes it mean anything.
    """
    seen = cancel_a_running_batch(tmp_path)

    assert seen["started"] == len(TASKS), (
        f"both items must be generating before the cancel; started {seen['started']}"
    )
    assert seen["cancelled"] == len(TASKS), (
        "cancelling the call must reach every gathered item; "
        f"{seen['cancelled']} of {seen['started']} saw CancelledError"
    )
    assert seen["error"] == "McpError", (
        f"the caller must be told the call was cancelled, got {seen['error']}"
    )


def test_a_cancelled_batch_gives_back_every_slot(tmp_path):
    """The consequence: the lockout was capacity held after the caller had gone.

    Process-local and cross-process both, because they are counted separately and what the
    incident did was refuse every other session on the machine, which only the second sees.
    """
    admission = cancel_a_running_batch(tmp_path)["status"]["admission"]

    assert admission["peak_inflight_seqs"] == len(TASKS), (
        f"the items must have held slots at all, or this asserts nothing: {admission}"
    )
    assert admission["inflight_seqs"] == 0, admission
    assert admission["inflight_tokens"] == 0, admission
    assert admission["inflight_large_prefills"] == 0, admission
    assert admission["per_entry"]["flash"]["inflight"] == 0, admission

    shared = admission["cross_process"]
    if shared["active"]:
        # Inactive on Windows, where there is no fcntl to lock the file with, so this
        # half runs only in WSL -- which is where the server actually runs.
        assert shared["inflight_seqs"] == 0, shared
        assert shared["processes_holding_slots"] == 0, shared


def test_a_cancelled_batch_still_records_what_it_did(tmp_path):
    """An abandoned delegation that writes nothing is unrecoverable, which is how one came
    to be listed as live for ever. The record is written in a `finally`, so cancellation
    must not be the one exit that skips it.
    """
    records = cancel_a_running_batch(tmp_path)["records"]

    assert len(records) == len(TASKS), f"one record per item, got {len(records)}"
    for record in records:
        assert record["ok"] is False, record
        assert record["error_type"] == "CancelledError", record
