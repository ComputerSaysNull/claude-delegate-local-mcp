"""The `alive` event counts chunks of both thinking and answering as one number.

A watcher following a delegation sees `chunks_seen` climb and has to guess what the model
is doing. The accumulator knows which half of the reply a frame carried -- it keeps
`_reasoning` and `_content` apart -- but `feed()` collapsed that to one bool, so the split
died at the adapter's door. This is the same number twice, and the only use of it is to
tell "the model is reasoning" from "it is answering", which is the one thing a collapsed
count cannot do.

The fix names the kind at the seam. `feed()` returns it, `on_token` carries it, the loop
tallies it separately, and the viewer says both numbers.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from wire_double import Clock, delta, paced

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_delegate_local import transcript
from claude_delegate_local.backends import openai_compat as oc

pytestmark = pytest.mark.anyio


def load_viewer():
    """Import scripts/watch_delegations.py the way tests/test_watch_delegations.py does."""
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "watch_delegations", root / "scripts" / "watch_delegations.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_accumulator_names_the_kind_of_each_frame():
    """Reasoning, content and tool-call fragments are the three things the split is for.

    The empty bookkeeping frame is the control: a role preamble or a usage frame carries no
    generated output, so it must report None rather than pretending to be either kind.
    """
    acc = oc._StreamAccumulator()
    assert acc.feed(delta(reasoning="thinking")) == "reasoning"
    assert acc.feed(delta(content="answer")) == "answer"
    assert acc.feed(delta(tool_calls=[
        {"index": 0, "function": {"name": "read_file", "arguments": ""}}
    ])) == "answer"
    assert acc.feed({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}) is None


async def test_the_adapter_reports_the_kind_of_each_arrival():
    """End to end from the wire, so the kind is not merely plumbed but correct.

    Three reasoning frames then two content frames: the callback sees the split in order,
    which is exactly what a watcher needs to see thinking end and answering begin.
    """
    from test_backends_openai_compat import backend, request

    clock = Clock()
    seen: list[str] = []
    schedule = [
        (1.0, "data: " + json.dumps(delta(reasoning="a")) + "\n\n"),
        (2.0, "data: " + json.dumps(delta(reasoning="b")) + "\n\n"),
        (3.0, "data: " + json.dumps(delta(reasoning="c")) + "\n\n"),
        (4.0, "data: " + json.dumps(delta(content="d")) + "\n\n"),
        (5.0, "data: " + json.dumps(delta(content="e")) + "\n\n"),
        (5.0, "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 9, "completion_tokens": 5}}) + "\n\n"),
        (5.0, "data: [DONE]\n\n"),
    ]

    await backend(paced(clock, schedule), clock=clock).complete(
        request(), on_token=seen.append
    )

    assert seen == ["reasoning"] * 3 + ["answer"] * 2


def test_the_alive_event_splits_the_chunks_by_kind(tmp_path):
    """The transcript keeps the total for old readers and adds the two halves.

    `answer_chunks` is derived rather than recorded, so the three can never disagree: the
    total is what was counted and the split is what it was split into.
    """
    stream = transcript.Stream(tmp_path / "alive.jsonl")
    stream.alive(elapsed_seconds=12.0, of_seconds=100, ends_in_seconds=88.0,
                 chunks_seen=1234, reasoning_chunks=1100, since_chunk_seconds=0.5)

    (line,) = (tmp_path / "alive.jsonl").read_text(encoding="utf-8").splitlines()
    event = json.loads(line)

    assert event["chunks_seen"] == 1234
    assert event["reasoning_chunks"] == 1100
    assert event["answer_chunks"] == 134
    assert event["reasoning_chunks"] + event["answer_chunks"] == event["chunks_seen"]


def test_the_viewer_shows_the_split_when_it_is_there_and_the_old_line_when_not():
    """Present shows both halves; absent keeps the old text, so an older stream reads on.

    The absent case is the one worth asserting separately: a transcript written before the
    split existed must not render as zero thinking and zero answering, which would claim a
    run nobody can re-measure reasoned not at all.
    """
    viewer = load_viewer()

    present = viewer._alive_line({
        "elapsed_seconds": 41.0, "of_seconds": 3600, "ends_in_seconds": 120.0,
        "chunks_seen": 1234, "reasoning_chunks": 1100, "since_chunk_seconds": 0.2,
    })
    assert "1,234 chunks" in present
    assert "(1,100 thinking, 134 answering)" in present

    absent = viewer._alive_line({
        "elapsed_seconds": 41.0, "of_seconds": 3600, "ends_in_seconds": 120.0,
        "chunks_seen": 3210, "since_chunk_seconds": 0.2,
    })
    assert "3,210 chunks" in absent
    assert "thinking" not in absent
    assert "answering" not in absent
