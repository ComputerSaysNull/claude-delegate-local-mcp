"""The append-only token ledger, one line per dispatch.

The per-dispatch transcript records are one file each and may be pruned. This ledger is
the running total that must never be, so what is asserted here is the append contract:
one line per dispatch, never torn, never raising. The concurrency guarantee is asserted
the same way it was measured -- many processes, many appends, and every line surviving.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError

from claude_delegate_local import ledger
from claude_delegate_local.config import Config

from test_server import (
    chat_reply,
    delegated,
    tool_call_reply,
    turn_handler,
)
from test_server import cfg as server_cfg

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "CONCURRENCY UNPROVEN BY THIS RUN -- not a pass. It uses multiprocessing with the "
        "fork context and POSIX files, and the server runs in WSL. Run: wsl -d "
        "Ubuntu-24.04 -e bash -lc 'cd <repo> && <python> -m pytest "
        "tests/test_ledger.py'"
    ),
)


def cfg(**over) -> Config:
    """A config rooted at the current directory, with an empty ledger unless asked."""
    return server_cfg(**over)


def ledger_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ---- the append contract -------------------------------------------------------------


def test_append_writes_one_line_that_parses_back(tmp_path):
    target = tmp_path / "ledger.jsonl"
    record = {"at": "2026-01-01T00:00:00+00:00", "ok": True, "model_key": "flash"}
    ledger.append(cfg(ledger_path=str(target)), record)
    lines = ledger_lines(target)
    assert len(lines) == 1
    assert lines[0] == record


def test_two_appends_give_two_lines(tmp_path):
    target = tmp_path / "ledger.jsonl"
    for i in range(2):
        ledger.append(cfg(ledger_path=str(target)), {"i": i})
    assert [line["i"] for line in ledger_lines(target)] == [0, 1]


def test_an_empty_ledger_path_writes_nothing(tmp_path):
    ledger.append(cfg(ledger_path=""), {"i": 0})
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_path_does_not_raise(tmp_path):
    """A full disk, or a parent that is a file, must not fail a delegation."""
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    ledger.append(cfg(ledger_path=str(blocker / "ledger.jsonl")), {"i": 0})
    # No exception is the assertion.


def test_a_path_that_cannot_be_translated_does_not_raise():
    """A UNC share has no mount point here. The setting is wrong; the delegation is not."""
    ledger.append(cfg(ledger_path=r"\\fileserver\share\ledger.jsonl"), {"i": 0})
    # No exception is the assertion.


def test_the_parent_directory_is_created(tmp_path):
    target = tmp_path / "not" / "yet" / "ledger.jsonl"
    ledger.append(cfg(ledger_path=str(target)), {"i": 0})
    assert ledger_lines(target) == [{"i": 0}]


# ---- the concurrency guarantee --------------------------------------------------------


def _append_worker(path_str: str, count: int) -> None:
    """Append `count` records from one process; run under the fork context."""
    local = Config(workspace_roots=(".",), ledger_path=path_str)  # type: ignore[arg-type]
    for i in range(count):
        ledger.append(local, {"i": i, "pid": os.getpid()})


@posix_only
def test_concurrent_appends_never_lose_a_line(tmp_path):
    """The single-write guarantee, measured rather than assumed.

    The measurement this protects against split the write into two syscalls and tore 57%
    of lines under 8 writers. Here 4 processes x 200 appends must leave every line
    intact and every record distinct.
    """
    target = tmp_path / "ledger.jsonl"
    path_str = str(target)
    count, nproc = 200, 4

    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_append_worker, args=(path_str, count)) for _ in range(nproc)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    lines = ledger_lines(target)
    assert len(lines) == nproc * count
    assert len({json.dumps(line, sort_keys=True) for line in lines}) == nproc * count


# ---- the server writes one line per delegation ----------------------------------------


def test_a_two_turn_delegation_writes_one_line_with_whole_run_totals(tmp_path):
    """The ledger's `total_*` are the whole-run figures, not one turn's.

    ADR-0058: a two-turn delegation used to report one turn's usage under lifetime names.
    The ledger must report the sum, so this reads the per-turn diagnostics and asserts the
    ledger line equals their total.
    """
    target = tmp_path / "ledger.jsonl"
    config = server_cfg(ledger_path=str(target), transcript_dir=str(tmp_path / "t"))

    result = delegated(
        turn_handler(
            tool_call_reply("read_file", {"path": "/nope"}),
            chat_reply(content="done"),
        ),
        config=config,
        task="go",
        allowed_tools=["read_file"],
        diagnostics=True,
    )

    lines = ledger_lines(target)
    assert len(lines) == 1
    line = lines[0]
    assert line["ok"] is True
    assert line["turns"] == 2
    assert line["effort"] == "low"
    per_turn = result["diagnostics"]["turns"]
    assert len(per_turn) == 2
    assert line["total_input_tokens"] == sum(t["input_tokens"] for t in per_turn)
    assert line["total_output_tokens"] == sum(t["output_tokens"] for t in per_turn)
    cached = [t.get("cached_tokens") for t in per_turn]
    if any(c is not None for c in cached):
        assert line["total_cached_tokens"] == sum(c or 0 for c in cached)
    else:
        assert line["total_cached_tokens"] is None


def test_a_failed_delegation_writes_one_line_with_ok_false(tmp_path):
    """A failure still gets its line, and its token fields are null, never 0."""
    target = tmp_path / "ledger.jsonl"
    config = server_cfg(ledger_path=str(target))

    def handler(request):
        raise httpx.ConnectError("connection failed")

    with pytest.raises(ToolError):
        delegated(handler, config=config, task="x")

    lines = ledger_lines(target)
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["total_input_tokens"] is None
    assert lines[0]["total_output_tokens"] is None
    assert lines[0]["total_cached_tokens"] is None
    assert lines[0]["model_key"] == "flash"
