"""The transcript's `answer` event says who answered the question.

Every `ask_caller` question goes to the caller (ADR-0124), so the stream's `answer` event
records `by: "caller"`; the schema keeps `person` only for older streams.
"""

from claude_delegate_local import transcript
from test_ask_caller_transcript import _drive
from test_ask_caller_server import scripted
from test_server import chat_reply, tool_call_reply
from test_transcript_format import errors, schema


def _answers(events: list[dict]) -> list[dict]:
    return [e for e in events if e["t"] == "answer"]


def test_a_caller_answer_is_marked_by_caller(tmp_path):
    """An ordinary question the caller answers through `answer`: `by: "caller"`."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    events = _drive(tmp_path, handler, text="foo.py")

    answers = _answers(events)
    assert len(answers) == 1, answers
    assert answers[0]["by"] == "caller", answers[0]
    assert errors(answers[0]) == [], errors(answers[0])


def test_a_best_reading_reply_is_marked_by_caller(tmp_path):
    """A `best_reading` reply is still the caller answering: `by: "caller"`."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="I choose foo.py"),
    )
    events = _drive(tmp_path, handler, best_reading=True)

    answers = _answers(events)
    assert len(answers) == 1, answers
    assert answers[0]["by"] == "caller", answers[0]
    assert answers[0]["best_reading"] is True
    assert errors(answers[0]) == [], errors(answers[0])


def test_every_answer_event_validates_against_the_schema(tmp_path):
    """Every `answer` event the question flow writes matches the shipped schema, the way
    test_ask_caller_transcript does -- here for the caller-answered path too."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    events = _drive(tmp_path, handler, text="foo.py")

    bad = {e["t"]: errors(e) for e in events if errors(e)}
    assert bad == {}


def test_the_schema_defines_by_since_1_10():
    """Pinned by kind, not by the current version: a later minor bump must not break it."""
    defs = schema()["$defs"]
    by = defs["answer"]["properties"]["by"]
    assert by["enum"] == ["caller", "person"]
    assert "1.10" in by["description"]
    assert "by" not in defs["answer"].get("required", [])
    major, minor = (int(part) for part in transcript.FORMAT.split("."))
    assert (major, minor) >= (1, 10)
