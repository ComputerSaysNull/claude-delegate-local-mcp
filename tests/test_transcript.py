"""The operator dispatch transcript. ADR-0024.

Upstream shipped this twice-broken, and both bugs are the acceptance criteria rather than
history: failure paths lost the agent name, so the very dispatches it existed to explain
logged as unknown; and the success path merged its payload into ordinary responses once
the directory was set. Each has a regression test named after it in `tests/regression/`.

What is asserted here is the rest of the contract -- that it is genuinely off by default,
that "on" is not merely non-crashing, that the record carries real usage rather than the
admission estimate, and that a write that fails takes nothing down with it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from claude_delegate_local import server
from claude_delegate_local.config import Config

from test_server import (
    DoubleCache,
    cfg,
    chat_reply,
    entry,
    files_cfg,
    files_posix_only,
    registry,
)


def run(handler, *, config: Config, tool: str = "delegate", **kwargs) -> dict:
    entries = (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    # `effort` is required on every delegation tool, and "inherit" is the value that
    # preserves the pre-ADR-0045 behaviour exactly: it falls through to the agent, the
    # registry row and then `thinking_default`. A test that cares about the level sets
    # it itself; that it is *refused* when absent is asserted directly, not here.
    kwargs.setdefault("effort", "inherit")

    async def go():
        async with Client(mcp) as client:
            return (await client.call_tool(tool, kwargs)).data

    return asyncio.run(go())


def ok(**over):
    async def handler(request):
        return httpx.Response(200, json=chat_reply(**over))

    return handler


def records(directory: Path) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.json"))
    ]


# ---- off by default, and not merely non-crashing when on ---------------------------
def test_nothing_is_written_when_no_directory_is_configured(tmp_path):
    """Off by default. The directory here is real and stays empty."""
    run(ok(), config=cfg(), task="a question")
    assert records(tmp_path) == []


def test_a_configured_directory_gets_a_complete_record(tmp_path):
    """The other half of "off by default is not the same as inert when on".

    Asserting the fields individually rather than that a file exists: a record written
    with everything null is a file, and would pass the weaker check while explaining
    nothing.
    """
    run(
        ok(content="the answer"),
        config=cfg(transcript_dir=str(tmp_path)),
        task="a question",
    )
    written = records(tmp_path)
    assert len(written) == 1
    one = written[0]

    assert one["task"] == "a question"
    assert one["ok"] is True
    assert one["model_key"] == "flash"
    assert one["input_tokens"] == 7
    assert one["output_tokens"] == 3
    assert one["answer_chars"] == len("the answer")
    assert one["elapsed_seconds"] >= 0
    assert one["admission"]["estimated_tokens"] > 0


def test_the_directory_is_created_if_it_does_not_exist(tmp_path):
    """An operator sets a path, not a path that already exists."""
    target = tmp_path / "not" / "yet" / "there"
    run(ok(), config=cfg(transcript_dir=str(target)), task="x")
    assert len(records(target)) == 1


# ---- what a record may and may not contain ------------------------------------------
def test_a_record_carries_real_usage_not_the_admission_estimate(tmp_path):
    """The estimate is a guess made before the work; usage is what the work cost.

    They must not be confused, because summing usage across records is the only way to
    answer what the cluster has actually spent -- an estimate standing in would poison
    that total silently, being the right shape and the wrong number.
    """
    run(
        ok(usage={"prompt_tokens": 1234, "completion_tokens": 56}),
        config=cfg(transcript_dir=str(tmp_path)),
        task="x",
    )
    one = records(tmp_path)[0]
    assert one["input_tokens"] == 1234
    assert one["output_tokens"] == 56
    assert one["admission"]["estimated_tokens"] != 1234


@files_posix_only
def test_file_contents_never_reach_a_record(tmp_path):
    """Paths and cost, not text. Bodies are recoverable from the repository by path, and
    writing them would put every prefetched file on disk at rest indefinitely."""
    target = tmp_path / "note.md"
    target.write_text("SENTINEL-CONTENT-XYZ\n", encoding="utf-8")
    out = tmp_path / "out"

    run(
        ok(),
        config=files_cfg(tmp_path, transcript_dir=str(out)),
        task="x",
        files=[str(target)],
    )
    one = records(out)[0]

    assert one["files_read"], "the file was not read at all; the test proves nothing"
    assert "note.md" in json.dumps(one), "the path should be recorded"
    assert "SENTINEL-CONTENT-XYZ" not in json.dumps(one)


# ---- the transcript must never be able to break a delegation -------------------------
def test_a_write_failure_does_not_fail_the_delegation(tmp_path):
    """A full disk must not fail work that already succeeded.

    The delegation is done by the time the record is written, so losing the record is
    worth strictly less than losing the answer.
    """
    blocker = tmp_path / "a-file-where-a-directory-should-be"
    blocker.write_text("x", encoding="utf-8")

    result = run(
        ok(content="still fine"),
        config=cfg(transcript_dir=str(blocker / "sub")),
        task="x",
    )
    assert result["answer"] == "still fine"




def test_per_turn_records_reach_the_transcript_though_the_caller_never_asked(tmp_path):
    """ADR-0024's actual requirement: independent of any caller-facing flag.

    The loop only *keeps* per-turn records when it is told to -- `_Watch.turn_cost`
    returns immediately otherwise -- so a transcript that merely read whatever the caller
    happened to request would be empty for every delegation that did not ask for
    diagnostics, which is nearly all of them. It has to ask for itself.

    `diagnostics` is deliberately not passed below. That is the whole test.
    """
    run(
        ok(),
        config=cfg(transcript_dir=str(tmp_path)),
        task="x",
        allowed_tools=["read_file"],
    )
    one = records(tmp_path)[0]
    assert one["turns"] >= 1
    assert one["per_turn"], "the transcript recorded no per-turn detail"
    assert one["per_turn"][0]["input_tokens"] == 7


posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits. Windows has no 0o600, and the server runs in WSL anyway.",
)


@posix_only
def test_a_transcript_is_not_readable_by_anyone_but_its_owner(tmp_path):
    """Both files were created at whatever the umask happened to allow.

    ADR-0043 put full model replies in the stream, and a record carries the task and the
    paths it was handed, so on a shared machine or under a loose umask this was at-rest
    exposure left to chance. Asserted as a property -- no group or other bits -- rather
    than as the literal 0o600: umask can only clear bits, so a stricter umask must not
    read as a failure while a laxer one still must.
    """
    directory = tmp_path / "t"
    run(ok(), config=cfg(transcript_dir=str(directory)), task="x")

    written = sorted(directory.glob("*.json")) + sorted(directory.glob("*.jsonl"))
    assert written, "expected a record and a stream to have been written"
    for f in written:
        mode = f.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"{f.name} is {oct(mode)}, readable beyond its owner"
        assert mode & 0o400, f"{f.name} is {oct(mode)}, unreadable by its own owner"

    assert directory.stat().st_mode & 0o077 == 0, "the directory itself is not private"
