"""A finished turn's thinking is complete in the stream, closed by a `final` partial.

While a turn streams, `partial` events carry the reply's text since the last one, at most
once per interval. Whatever arrived after the last emit was never written, so joining a
turn's partials missed the tail of its thinking. The loop now flushes what `PartialText`
still holds as one last `partial` marked `final: true`, written immediately before the
turn's `turn` event: joined in order, a turn's partials then hold all of its reasoning and
reply text (format 1.8).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from claude_delegate_local import transcript
from test_transcript_stream import _run
from wire_double import delta, sse, sse_response

SCHEMA = json.loads(Path(transcript.__file__).with_name("transcript.schema.json")
                    .read_text(encoding="utf-8"))


def pieces_reply(request):
    """A reply streamed in several pieces, reasoning first, as a thinking model sends it.

    The last interval emit happens after "Thinking " (the first piece, which goes at once),
    so the rest of the reasoning and all of the answer are the tail a reader used to lose.
    """
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


def _turn_and_partials(events):
    partials = [e for e in events if e["t"] == "partial"]
    turn = next(e for e in events if e["t"] == "turn")
    return partials, turn


def test_the_agentic_path_flushes_the_thinking_tail_as_a_final_partial(tmp_path):
    """Reasoning streamed in several chunks, with the tail arriving after the last emit."""
    events = _run(tmp_path, pieces_reply, "delegate", {"task": "explain the retry"},
                  partial_every_seconds=3600.0)
    partials, turn = _turn_and_partials(events)
    assert partials, [e["t"] for e in events]
    # Joined in order, the turn's partials hold the full reasoning the backend streamed.
    assert "".join(p["reasoning"] for p in partials) == "Thinking it over. "
    # ... and the full reply text, so a reader is never missing the tail of either half.
    assert "".join(p["answer"] for p in partials) == "The retry is bounded by time."
    assert turn["text"].startswith("".join(p["answer"] for p in partials))
    # The last partial is the final one, and lands immediately before the `turn` event.
    assert partials[-1]["final"] is True
    assert events.index(partials[-1]) == events.index(turn) - 1
    assert partials[-1]["turn"] == turn["turn"]


def test_the_one_shot_path_flushes_the_thinking_tail_as_a_final_partial(tmp_path):
    """An empty toolset runs the one-shot path, which shares the same partial machinery."""
    events = _run(tmp_path, pieces_reply, "delegate",
                  {"task": "explain the retry", "allowed_tools": []},
                  partial_every_seconds=3600.0)
    partials, turn = _turn_and_partials(events)
    assert partials, [e["t"] for e in events]
    assert "".join(p["reasoning"] for p in partials) == "Thinking it over. "
    assert "".join(p["answer"] for p in partials) == "The retry is bounded by time."
    assert partials[-1]["final"] is True
    assert events.index(partials[-1]) == events.index(turn) - 1
    assert partials[-1]["turn"] == turn["turn"]


def test_no_non_final_partial_carries_the_final_key(tmp_path):
    """Only the flushed tail says `final`; the streaming partials stay as they were."""
    events = _run(tmp_path, pieces_reply, "delegate", {"task": "explain the retry"},
                  partial_every_seconds=3600.0)
    partials, _ = _turn_and_partials(events)
    assert len(partials) >= 2, partials
    assert all("final" not in p for p in partials[:-1])
    assert "final" in partials[-1]


def test_with_partials_disabled_no_partial_at_all_is_written(tmp_path):
    """The negative control: the same delegation, the setting at zero, so no stray final."""
    events = _run(tmp_path, pieces_reply, "delegate", {"task": "explain"},
                  partial_every_seconds=0.0)
    assert [e for e in events if e["t"] == "partial"] == []


def test_a_final_partial_matches_the_schema():
    event = {"t": "partial", "at": "2026-10-01T12:00:00+00:00", "turn": 1,
             "reasoning": "it over. ", "answer": "The retry is bounded by time.",
             "final": True}
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert list(validator.iter_errors(event)) == []


def test_the_format_claims_final_partials():
    """A minor-version bump, so a reader can rely on the flag (ADR-0111)."""
    assert "final" in SCHEMA["$defs"]["partial"]["properties"]
    assert SCHEMA["$defs"]["partial"]["properties"]["final"]["type"] == "boolean"
    assert "format 1.8" in SCHEMA["$defs"]["partial"]["properties"]["final"]["description"]
    major, minor = (int(part) for part in transcript.FORMAT.split("."))
    assert (major, minor) >= (1, 8)
