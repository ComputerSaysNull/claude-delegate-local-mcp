"""The stream's format is a contract: versioned, and pinned by a schema that ships.

Its reader is about to live in another repository and another language, and such a reader
cannot tell an older shape from a broken one unless the stream says which shape it is. So
`start` names the format, `transcript.schema.json` defines it, and the samples beside this
file are what a reader's own tests can be built on (ADR-0111).

Every check here is also run backwards: a schema that accepts everything would pass the
forward half, so each negative test asserts a real violation is refused.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

from claude_delegate_local import transcript
from test_server import chat_reply
from test_transcript_stream import _run, two_turns
from wire_double import as_stream

SCHEMA_PATH = Path(transcript.__file__).with_name("transcript.schema.json")
SAMPLES = sorted((Path(__file__).parent / "transcript_samples").glob("*.jsonl"))


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def errors(event: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema())
    return [e.message for e in validator.iter_errors(event)]


def sample_events() -> list[dict]:
    return [json.loads(line) for p in SAMPLES
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def first(kind: str) -> dict:
    return copy.deepcopy(next(e for e in sample_events() if e["t"] == kind))


def test_the_schema_is_itself_valid():
    jsonschema.Draft202012Validator.check_schema(schema())


def test_the_start_event_names_the_format_it_is_written_in(tmp_path):
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    assert events[0]["t"] == "start"
    assert events[0]["format"] == transcript.FORMAT


@pytest.mark.parametrize(("tool", "handler"), [
    ("delegate", two_turns),
    ("delegate_readonly", lambda r: as_stream(chat_reply(content="a one-shot answer"))),
])
def test_every_event_a_real_delegation_writes_matches_the_schema(tmp_path, tool, handler):
    events = _run(tmp_path, handler, tool, {"task": "explain the retry"})
    bad = {e["t"]: errors(e) for e in events if errors(e)}
    assert bad == {}


def test_every_sample_matches_the_schema():
    assert SAMPLES, "no samples found"
    bad = [(e["t"], errors(e)) for e in sample_events() if errors(e)]
    assert bad == []


def test_the_samples_hold_every_kind_the_schema_defines():
    """A kind with no sample is a kind a reader's tests never see."""
    defined = {re.search(r'"const": "(\w+)"', json.dumps(b["if"])).group(1)
               for b in schema()["allOf"]}
    assert defined <= {e["t"] for e in sample_events()}


def test_the_schema_and_the_code_name_the_same_version():
    assert schema()["x-transcript-format"] == transcript.FORMAT
    major = transcript.FORMAT.split(".")[0]
    assert schema()["$defs"]["start"]["properties"]["format"]["pattern"].startswith(f"^{major}\\.")


def test_a_start_without_its_format_is_refused():
    start = first("start")
    del start["format"]
    assert errors(start)


def test_an_unknown_major_version_is_refused():
    start = first("start")
    start["format"] = "2.0"
    assert errors(start)


def test_a_newer_minor_version_and_an_unknown_field_are_both_accepted():
    """What lets a reader pinned to 1.0 keep working while the writer adds things."""
    start = first("start")
    major, minor = (int(part) for part in transcript.FORMAT.split("."))
    start["format"] = f"{major}.{minor + 1}"
    start["something_new"] = {"any": "shape"}
    assert errors(start) == []
    assert errors({"t": "a_kind_from_the_future", "at": "2026-10-01T00:00:00+00:00"}) == []


@pytest.mark.parametrize(("kind", "field"), [
    ("turn", "text"), ("end", "ok"), ("alive", "chunks_seen"), ("tools", "tool_calls"),
])
def test_an_event_missing_a_required_field_is_refused(kind, field):
    event = first(kind)
    del event[field]
    assert errors(event)


def test_a_field_of_the_wrong_type_is_refused():
    end = first("end")
    end["ok"] = "yes"
    assert errors(end)
