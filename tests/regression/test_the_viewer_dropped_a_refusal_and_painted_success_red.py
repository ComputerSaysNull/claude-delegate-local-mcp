"""Two defects in one line of the transcript viewer, found only when something new
had to be shown there.

The renderer interpolated `call.get('detail', '')` into every tool-call line, and no
producer of a call entry has ever written a `detail` key -- the only `detail` in the
project is on a `backend_status` row. And its sole test of success was
`outcome == "ok"`, which is not in the vocabulary `_run_calls` produces: `ran`,
`repeat` and `error`. So a healthy call rendered red with the word `ran` beside it.

Neither could be caught by the existing suite, because nothing asserted on that line at
all -- the viewer's 1,217-test run passed identically before and after the fix. That is
the fifth and sixth check-that-cannot-fire in this repository, and both survived in a
renderer for the same reason: colour is looked at, not asserted on. Hence this file.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "scripts" / "watch_delegations.py"
_ANSI = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


@pytest.fixture
def viewer():
    spec = importlib.util.spec_from_file_location("watch_delegations", VIEWER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def turn(*calls: dict) -> dict:
    return {
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 100, "output_tokens": 5, "tool_calls": list(calls),
    }


def test_a_refused_call_shows_the_reason_it_refused(viewer):
    """The whole point of the record. A delegation reporting one error across twelve
    `read_git` calls could not be diagnosed, and a viewer that drops the message
    reproduces that failure in the one place a person is actually watching."""
    screen = "\n".join(viewer.render(turn({
        "name": "read_git", "outcome": "error",
        "arguments": {"repo": ".", "command": "log -1"},
        "message": "read_git is not available in this delegation.",
    }), 100))

    assert "read_git is not available in this delegation." in screen
    assert "command=log -1" in screen, "and what it was asked, not only that it failed"


def test_the_refusal_itself_is_red_not_only_the_outcome_word(viewer):
    """The outcome was coloured and the reason for it was not, which is the wrong way
    round: `error` is one token a reader can find anywhere on the line, and the message is
    what they came for. Asserted on the message line rather than the head line, so a
    renderer that colours only the outcome cannot pass this."""
    lines = viewer.render(turn({
        "name": "read_git", "outcome": "error", "arguments": {"repo": "."},
        "message": "read_git is not available in this delegation.",
    }), 100)
    message_line = next(line for line in lines if "not available" in line)

    assert viewer.RED in message_line, message_line


def test_a_blank_line_inside_a_refusal_carries_no_colour_codes(viewer):
    """A real path refusal wraps across paragraphs. Colouring an empty line would emit
    escape codes around nothing, which is invisible until someone pipes the viewer's
    output somewhere that shows them."""
    lines = viewer.render(turn({
        "name": "read_file", "outcome": "error", "arguments": {"path": "/nope"},
        "message": "The `path` argument was refused:\n\n  /nope\n    outside every root.",
    }), 100)

    assert any(line == "" for line in lines), "the blank line survived the wrap"
    assert all(_ANSI.sub("", line) != "" or line == "" for line in lines)


def test_a_successful_call_is_not_painted_as_a_failure(viewer):
    """The negative direction, and the defect itself: `ran` is a success and must not be
    coloured like an error. Asserting on the escape sequence rather than on the word,
    because the word was always right and the colour was always wrong."""
    lines = viewer.render(turn({
        "name": "read_file", "outcome": "ran",
        "arguments": {"path": "src/x.py"}, "result_bytes": 2100, "result_lines": 84,
    }), 100)
    call_line = next(line for line in lines if "read_file" in line)

    assert viewer.GREEN in call_line, call_line
    assert viewer.RED not in call_line, "a call that ran is not an error"


def test_an_error_is_still_painted_as_one(viewer):
    """The other half of the pair above. A colour test that only proves green appears
    would pass a renderer that painted everything green."""
    lines = viewer.render(turn({
        "name": "run_bash", "outcome": "error",
        "arguments": {"command": "false"}, "message": "exited 1", "exit_code": 1,
    }), 100)
    call_line = next(line for line in lines if "run_bash" in line)

    assert viewer.RED in call_line, call_line


def test_the_renderer_reads_no_key_no_producer_writes(viewer):
    """`detail` was interpolated into every call line and never written by anything. A
    dead read is invisible in a renderer, so this asserts on the source: the guard has
    to be that the key is gone, because no rendered output could ever have shown it."""
    source = VIEWER.read_text(encoding="utf-8")

    assert "detail" not in source, "the only `detail` in the project is a backend_status row"


def test_success_accounting_is_shown_and_absent_when_not_recorded(viewer):
    """Accounting is what a successful call carries instead of content. An older
    transcript has none, and rendering that as `0 B` would claim a measurement nobody
    made -- the same absent-is-not-empty rule the record itself follows."""
    with_counts = "\n".join(viewer.render(turn({
        "name": "search_files", "outcome": "ran", "arguments": {"pattern": "x"},
        "result_bytes": 640, "result_lines": 12,
    }), 100))
    without = "\n".join(viewer.render(turn({
        "name": "search_files", "outcome": "ran", "arguments": {"pattern": "x"},
    }), 100))

    assert "640 B" in with_counts and "12 lines" in with_counts
    assert "B" not in _ANSI.sub("", without).split("search_files")[1]


def test_a_narrow_terminal_drops_the_arguments_and_never_the_refusal(viewer):
    """What gives when there is no room. The refusal is the reason the record exists, so
    it wraps onto its own lines; the arguments move to a line of their own rather than
    being cut, so nothing is silently lost either way."""
    screen = _ANSI.sub("", "\n".join(viewer.render(turn({
        "name": "read_git", "outcome": "error",
        "arguments": {"repo": "." * 60, "command": "log " + "-1 " * 20},
        "message": "a refusal long enough that it cannot share the head line at all",
    }), 48)))

    assert "a refusal long enough" in screen
    assert "log -1" in screen, "the arguments wrapped rather than vanished"
