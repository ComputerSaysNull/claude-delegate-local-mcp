"""A first turn that ran tools still charged the dispatch's setup to them.

`tool_clock` started when the slot was granted, so the first turn's window held budget
pricing and request assembly as well as the tools -- the number a closing summary reported
as "tool time" was partly the server's own bookkeeping, and a delegation that called no
tools at all still reported seconds of it.

The fix moves the measurement to where the tool executes: each call carries its own `ms`,
and the server sums those. A turn that ran tools is now exactly as long as its calls took,
however long the dispatch spent pricing the budget or writing the transcript around them.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import time

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import ToolResultBlock, ToolUseBlock
from claude_delegate_local.config import Config
from claude_delegate_local.server import tool_ms_for_turn
from claude_delegate_local.tools import ALL_TOOL_NAMES, BashPolicy


class _Call:
    """The subset of a tool-call record `tool_ms_for_turn` reads."""

    def __init__(self, *, ms):
        self.ms = ms


class _Diagnostic:
    """The subset of a turn diagnostic the accumulator reads."""

    def __init__(self, *, tool_calls=()):
        self.tool_calls = tuple(tool_calls)


def test_a_single_calls_own_ms_is_the_turns_tool_time():
    """One tool call that took a second reports a second, however long the turn was.

    The turn's wall clock is no longer an input, so a slow dispatch -- budget pricing,
    request assembly, a transcript write -- cannot inflate the figure the way it used to.
    """
    assert tool_ms_for_turn(_Diagnostic(tool_calls=(_Call(ms=1000),))) == 1000


def test_a_turn_that_ran_no_tools_reports_zero():
    """The negative control, and the defect's shape at its smallest."""
    assert tool_ms_for_turn(_Diagnostic(tool_calls=())) == 0


@pytest.mark.skipif(
    os.name != "posix",
    reason="UNPROVEN ON WINDOWS -- not a pass: layer 1 refuses every path under tmp_path there.",
)
def test_a_real_run_records_each_calls_own_ms(tmp_path):
    """Driving `_run_calls` for real over one cheap tool yields a measured `ms`.

    Not asserted on a mocked timer: the point is that the record carries the number the
    server measured at execution, and the only way to prove it does is to run one.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    cfg = Config(workspace_roots=(str(tmp_path),), respect_gitignore=False)
    call = ToolUseBlock(id="a", name="read_file", input={"path": str(tmp_path / "a.py")})

    _, records = loop._run_calls(
        cfg, (call,), frozenset(ALL_TOOL_NAMES), {}, loop._Watch(diagnostics=True),
        policy=BashPolicy(),
    )

    assert isinstance(records[0].ms, int)
    assert records[0].ms >= 0
    assert records[0].outcome == "ran"


def test_overlapped_calls_are_each_timed_around_their_own_work(monkeypatch, tmp_path):
    """Two cacheable calls go through the thread pool, and each must time its own work.

    A clock read before the call rather than after it reports 0 for every call, which the
    `>= 0` above cannot tell from a real measurement. So the work here takes known time.
    """
    def slow(cfg, call, allowed, policy):
        time.sleep(0.05)
        return ToolResultBlock(tool_use_id=call.id, content="ok")

    monkeypatch.setattr(loop, "execute_tool", slow)
    calls = tuple(
        ToolUseBlock(id=name, name="read_file", input={"path": f"/nowhere/{name}.py"})
        for name in ("a", "b")
    )

    _, records = loop._run_calls(
        Config(workspace_roots=(str(tmp_path),)), calls, frozenset(ALL_TOOL_NAMES), {},
        loop._Watch(diagnostics=True), policy=BashPolicy(),
    )

    assert [r.outcome for r in records] == ["ran", "ran"]
    assert all(r.ms >= 40 for r in records), [r.ms for r in records]


@pytest.fixture()
def viewer():
    """The same import the viewer's own suite uses."""
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "scripts" / "watch_delegations.py")
    spec = importlib.util.spec_from_file_location("watch_delegations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_turn_line_uses_the_calls_own_ms_when_present(viewer):
    """The viewer's half: a turn's tool time is the sum its calls measured for themselves."""
    tool_ms = viewer._turn_tool_ms({
        "tool_calls": [
            {"name": "read_file", "outcome": "ran", "ms": 600},
            {"name": "search_files", "outcome": "ran", "ms": 400},
        ],
    })

    assert tool_ms == 1000


def test_the_turn_line_falls_back_to_derivation_for_old_transcripts(viewer):
    """A transcript written before the calls carried `ms` still renders tool time."""
    tool_ms = viewer._turn_tool_ms({
        "tool_calls": [{"name": "read_file", "outcome": "ran"}],
        "ms": 300_000, "backend_ms": 120_000,
    })

    assert tool_ms == 180_000
