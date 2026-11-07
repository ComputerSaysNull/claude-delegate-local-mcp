"""A swapped engine answered under the old model's name, and the stream could not tell.

The stream recorded only `start.model_key`, the registry key. An engine that answers a
request naming a model it does not serve -- unlike one that refuses it -- left every run
labelled with the stale entry. The stream now records the model each turn was answered
by, and `end` says whether it differed from the one requested.
"""

import json

import httpx

from test_server import chat_reply, tool_call_reply
from test_transcript_format import errors, first
from test_transcript_stream import _run
from wire_double import as_stream


def _answered_as(model: str):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(m.get("role") == "tool" for m in body.get("messages", [])):
            return as_stream(chat_reply(content="done", model=model))
        return as_stream({**tool_call_reply("read_file", {"path": "/nope.py"}), "model": model})
    return handler


def _turns(events):
    return [e for e in events if e["t"] == "turn"]


def test_each_turn_names_the_model_that_answered_and_end_flags_the_mismatch(tmp_path):
    events = _run(tmp_path, _answered_as("another-model"), "delegate", {"task": "t"})
    assert events[0]["served_model_id"] == "served-id-1"
    assert [t["model"] for t in _turns(events)] == ["another-model", "another-model"]
    assert events[-1]["model_mismatch"] is True


def test_the_requested_model_answering_is_no_mismatch(tmp_path):
    events = _run(tmp_path, _answered_as("served-id-1"), "delegate", {"task": "t"})
    assert [t["model"] for t in _turns(events)] == ["served-id-1", "served-id-1"]
    assert events[-1]["model_mismatch"] is False


def test_a_one_shot_turn_names_its_model_too(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return as_stream(chat_reply(content="sum", model="another-model"))
    events = _run(tmp_path, handler, "delegate", {"task": "t", "allowed_tools": []})
    assert [t["model"] for t in _turns(events)] == ["another-model"]
    assert events[-1]["model_mismatch"] is True


def test_the_schema_refuses_a_model_that_is_not_a_string():
    turn, end, start = first("turn"), first("end"), first("start")
    assert errors({**turn, "model": 5})
    assert errors({**end, "model_mismatch": "yes"})
    assert errors({**start, "served_model_id": 5})
    assert not errors({**turn, "model": "m"})
    assert not errors({**end, "model_mismatch": None})


def test_a_reply_naming_no_model_is_not_reported_as_the_requested_one(tmp_path):
    """The adapter fills `response.model` from the entry when the engine names none, so
    reading that field would invent a match nobody observed."""
    def handler(request: httpx.Request) -> httpx.Response:
        reply = chat_reply(content="sum")
        del reply["model"]
        return as_stream(reply)
    events = _run(tmp_path, handler, "delegate", {"task": "t", "allowed_tools": []})
    assert [t["model"] for t in _turns(events)] == [None]
    assert events[-1]["model_mismatch"] is None
