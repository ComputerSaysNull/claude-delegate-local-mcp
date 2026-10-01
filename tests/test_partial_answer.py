"""The reply as it generates, in the stream, so a watcher sees text and not only counters.

Until 1.1 the text reached the stream only in the `turn` event, once the reply was whole:
during a long turn a watcher saw chunk counts climb and nothing else (M19.5). `partial`
events now carry what arrived since the last one -- the first piece of a turn at once, then
at most one per `partial_every_seconds` -- and the `turn` event still carries the whole
reply, which is the one a reader keeps.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from claude_delegate_local import transcript, watch
from claude_delegate_local.loop import PartialText
from test_transcript_stream import _run
from wire_double import delta, sse, sse_response

SCHEMA = json.loads(Path(transcript.__file__).with_name("transcript.schema.json")
                    .read_text(encoding="utf-8"))


def pieces_reply(request):
    """A reply streamed in several pieces, reasoning first, as a thinking model sends it."""
    return sse_response(sse(
        {"model": "served-id-1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        delta(reasoning_content="Thinking "),
        delta(reasoning_content="it over. "),
        delta(content="The retry "),
        delta(content="is bounded "),
        delta(content="by time."),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 9}},
    ))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_the_first_piece_goes_at_once_and_then_one_per_interval():
    clock, sent = Clock(), []
    text = PartialText(2.0, lambda r, a: sent.append((r, a)), clock)
    text.add("reasoning", "Thinking ")          # first piece of the turn: at once
    clock.t = 0.5
    text.add("reasoning", "it over. ")
    clock.t = 1.0
    text.add("answer", "The retry ")            # inside the interval: held
    assert sent == [("Thinking ", "")]
    clock.t = 2.1
    text.add("answer", "is bounded ")           # interval passed: everything held goes
    assert sent == [("Thinking ", ""), ("it over. ", "The retry is bounded ")]


def test_a_new_turn_drops_what_the_last_one_still_held():
    """The `turn` event already carries the whole of that reply; carrying its tail into
    the next turn's first partial would put one turn's words under another's number."""
    clock, sent = Clock(), []
    text = PartialText(2.0, lambda r, a: sent.append((r, a)), clock)
    text.add("answer", "one ")
    clock.t = 0.5
    text.add("answer", "held")
    text.reset()
    clock.t = 0.6
    text.add("answer", "two")
    assert sent == [("", "one "), ("", "two")]


def test_zero_turns_it_off():
    sent: list = []
    text = PartialText(0.0, lambda r, a: sent.append((r, a)), Clock())
    text.add("answer", "anything")
    assert sent == []


def test_a_failing_writer_does_not_fail_the_turn():
    def broken(r, a):
        raise OSError("disk full")

    PartialText(1.0, broken, Clock()).add("answer", "x")  # must not raise


@pytest.mark.parametrize("tool", ["delegate_readonly", "delegate"])
def test_a_real_delegation_streams_partials_that_match_its_reply(tmp_path, tool):
    events = _run(tmp_path, pieces_reply, tool, {"task": "explain the retry"},
                  partial_every_seconds=3600.0)
    partials = [e for e in events if e["t"] == "partial"]
    turn = next(e for e in events if e["t"] == "turn")
    assert partials, [e["t"] for e in events]
    assert partials[0]["turn"] == turn["turn"]
    assert partials[0]["reasoning"] == "Thinking "
    # What was written as partial is the reply's own text, in order, never anything else.
    assert turn["text"].startswith("".join(p["answer"] for p in partials))
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert [list(validator.iter_errors(e)) for e in events] == [[] for _ in events]


def test_with_partials_off_the_stream_has_none(tmp_path):
    """The negative control: the same delegation, the setting at zero."""
    events = _run(tmp_path, pieces_reply, "delegate_readonly", {"task": "explain"},
                  partial_every_seconds=0.0)
    assert [e for e in events if e["t"] == "partial"] == []


def test_the_format_says_partials_may_appear():
    """An addition, so a minor version (ADR-0111)."""
    assert transcript.FORMAT == "1.1"


def test_the_terminal_viewer_shows_a_partial_as_one_line():
    event = {"t": "partial", "at": "2026-10-01T12:00:00+00:00", "turn": 2,
             "reasoning": "", "answer": "The retry is bounded by time."}
    lines = [watch._plain(line) for line in watch.render(event, 100)]
    assert len(lines) == 1
    assert "The retry is bounded by time." in lines[0]
