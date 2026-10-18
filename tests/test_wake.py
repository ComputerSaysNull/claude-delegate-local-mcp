"""The `wait-hook` subcommand: wake an idle session when a run needs its caller.

`delegate` answers with a handle at once, so a caller that ends its turn without holding a
`collect` or `answer` open hears nothing until the person writes (ADR-0126). Claude
Code's `asyncRewake` hook runs this in the background and, when it exits with code 2, its
stderr wakes the model. This module is pure and does not import the server or the loop: it
polls the per-handle state file `handles.py` writes and reports back.
"""

from __future__ import annotations

import json
import os

from claude_delegate_local.wake import (
    GRACE_SECONDS,
    decide,
    handle_from_hook,
    wait,
)


def _finished_state(handle, *, collected):
    return {"handle": handle, "pid": os.getpid(), "tool": "delegate",
            "status": "done", "questions_asked": 0, "questions_shown": 0,
            "collected": collected}


def _question_state(handle, *, asked, shown):
    return {"handle": handle, "pid": os.getpid(), "tool": "delegate",
            "status": "question", "questions_asked": asked, "questions_shown": shown,
            "collected": False}


def _write(tmp_path, handle, state):
    (tmp_path / f"{handle}.json").write_text(json.dumps(state))


class FakeClock:
    """A clock and a sleep that advance together, so `wait`'s polling is instant."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# --- handle_from_hook -------------------------------------------------------------


def test_handle_from_hook_reads_the_tool_result_as_json_text():
    payload = {"tool_response": json.dumps({"handle": "d-abc123", "status": "running"})}
    assert handle_from_hook(payload) == "d-abc123"


def test_handle_from_hook_reads_the_tool_result_as_a_dict():
    payload = {"tool_response": {"handle": "d-abc123", "status": "running"}}
    assert handle_from_hook(payload) == "d-abc123"


def test_handle_from_hook_reads_a_content_block_list():
    block = {"type": "text", "text": json.dumps({"handle": "d-abc123", "status": "running"})}
    payload = {"tool_response": [block]}
    assert handle_from_hook(payload) == "d-abc123"


def test_handle_from_hook_ignores_a_done_result():
    payload = {"tool_response": json.dumps({"handle": "d-abc123", "status": "done"})}
    assert handle_from_hook(payload) is None


def test_handle_from_hook_ignores_a_result_with_no_handle():
    payload = {"tool_response": json.dumps({"status": "running"})}
    assert handle_from_hook(payload) is None


# --- decide ------------------------------------------------------------------------


def test_decide_exits_when_there_is_no_state():
    assert decide(None, pid_alive=True, held_for=100.0) == ("exit", "")


def test_decide_exits_when_the_server_pid_is_dead():
    state = _finished_state("d-1", collected=False)
    assert decide(state, pid_alive=False, held_for=100.0) == ("exit", "")


def test_decide_exits_when_a_finished_run_was_collected():
    state = _finished_state("d-1", collected=True)
    assert decide(state, pid_alive=True, held_for=100.0) == ("exit", "")


def test_decide_keeps_waiting_until_the_grace_for_a_finished_run():
    state = _finished_state("d-1", collected=False)
    assert decide(state, pid_alive=True, held_for=GRACE_SECONDS - 1.0) is None
    assert decide(state, pid_alive=True, held_for=GRACE_SECONDS) == (
        "wake", ("A delegation finished and nobody has collected it: call `collect` "
                 "with handle d-1."))


def test_decide_keeps_waiting_until_the_grace_for_an_unread_question():
    state = _question_state("d-1", asked=1, shown=0)
    assert decide(state, pid_alive=True, held_for=GRACE_SECONDS - 1.0) is None
    assert decide(state, pid_alive=True, held_for=GRACE_SECONDS) == (
        "wake", ("A delegation is waiting on a question nobody has read: call `collect` "
                 "with handle d-1 to read it, then `answer`."))


def test_decide_keeps_waiting_when_the_question_was_shown():
    state = _question_state("d-1", asked=1, shown=1)
    assert decide(state, pid_alive=True, held_for=100.0) is None


# --- wait --------------------------------------------------------------------------


def test_wait_wakes_for_a_finished_uncollected_run_after_the_grace(tmp_path):
    handle = "d-abc123"
    _write(tmp_path, handle, _finished_state(handle, collected=False))
    clock = FakeClock()
    code, message = wait(handle, tmp_path, sleep=clock.sleep, clock=clock.clock,
                         alive=lambda pid: True)
    assert code == 2
    assert "collect" in message and handle in message
    assert clock.now >= GRACE_SECONDS


def test_wait_exits_when_the_run_is_collected_during_the_grace(tmp_path):
    handle = "d-abc123"
    _write(tmp_path, handle, _finished_state(handle, collected=False))
    clock = FakeClock()
    flipped = {"n": 0}

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        flipped["n"] += 1
        if flipped["n"] == 1:
            _write(tmp_path, handle, _finished_state(handle, collected=True))

    code, message = wait(handle, tmp_path, sleep=sleep, clock=clock.clock,
                         alive=lambda pid: True)
    assert code == 0
    assert message == ""
    assert clock.now < GRACE_SECONDS


def test_wait_wakes_for_an_unread_question_after_the_grace(tmp_path):
    handle = "d-abc123"
    _write(tmp_path, handle, _question_state(handle, asked=1, shown=0))
    clock = FakeClock()
    code, message = wait(handle, tmp_path, sleep=clock.sleep, clock=clock.clock,
                         alive=lambda pid: True)
    assert code == 2
    assert "question" in message and handle in message


def test_wait_returns_at_once_when_another_waiter_is_alive(tmp_path):
    handle = "d-abc123"
    (tmp_path / f"{handle}.waiter").write_text(str(os.getpid()))
    code, message = wait(handle, tmp_path)
    assert code == 0
    assert message == ""


def test_wait_exits_when_the_state_file_is_missing(tmp_path):
    handle = "d-abc123"
    code, message = wait(handle, tmp_path, alive=lambda pid: True)
    assert code == 0
    assert message == ""
