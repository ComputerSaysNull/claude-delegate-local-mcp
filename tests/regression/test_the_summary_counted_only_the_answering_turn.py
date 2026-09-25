"""The `.json` summary record cannot be summed to get cluster spend.

`transcript.write` builds the record's top-level `input_tokens`, `output_tokens` and
`cached_tokens` from `_usage(dispatched)`, which reads `dispatched.response` -- and for a
multi-turn (agentic) run that response describes only the turn that answered. Yet the
record's own contract says summing across records is how an operator answers what the
cluster actually spent. So anyone summing summaries undercounts every multi-turn
delegation: a twelve-turn run reported one turn's tokens under the names the record
says are the lifetime ones.

The whole-run figures already exist on the dispatch object (`total_input_tokens`,
`total_output_tokens`, `total_cached_tokens`); the summary record just never carried
them. Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json

from test_server import cfg

from claude_delegate_local import loop, transcript
from claude_delegate_local.backends.base import CanonicalResponse, TextBlock


def _reply(input_tokens: int, output_tokens: int, cached_tokens: int,
           text: str = "done") -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model="served-id-1",
        cached_tokens=cached_tokens,
    )


def _turn(n: int, input_tokens: int, output_tokens: int, cached_tokens: int):
    return loop.TurnDiagnostic(
        turn=n,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        attempts=1,
        effort="high",
        evicted=0,
        tool_calls=(),
    )


def _agentic() -> loop.AgenticDispatch:
    """Two turns with differing counts, so the answering turn is not the whole run.

    Turn one cost 200/80/40 and the answering turn cost 100/50/20, so the run cost
    300/130/60. The `total_*` fields on the dispatch carry the run; `response` carries
    only the answering turn.
    """
    return loop.AgenticDispatch(
        response=_reply(100, 50, 20),
        effort="high",
        attempts=1,
        turns=2,
        total_input_tokens=300,
        total_output_tokens=130,
        total_cached_tokens=60,
        diagnostics=(_turn(1, 200, 80, 40), _turn(2, 100, 50, 20)),
    )


def _write(tmp_path, dispatched) -> dict:
    config = cfg(transcript_dir=str(tmp_path))
    transcript.write(
        config, agent_name="docs-audit-local", entry=None, task="t", workdir=None,
        prefetched=None, lease=None, dispatched=dispatched, error=None, started=0.0,
    )
    return json.loads(next(iter(tmp_path.glob("*.json"))).read_text(encoding="utf-8"))


def test_the_summary_reports_the_whole_run_not_only_the_answering_turn(tmp_path):
    """The harm itself: summing the record undercounts a multi-turn delegation.

    The `total_*` fields must equal the sum over `per_turn` -- which is what the run
    really cost -- while the answering-turn names keep their meaning.
    """
    record = _write(tmp_path, _agentic())

    per_turn = record["per_turn"]
    assert sum(t["input_tokens"] for t in per_turn) == 300
    assert sum(t["output_tokens"] for t in per_turn) == 130
    assert sum(t["cached_tokens"] for t in per_turn) == 60

    assert record["total_input_tokens"] == sum(
        t["input_tokens"] for t in per_turn), record["total_input_tokens"]
    assert record["total_output_tokens"] == sum(
        t["output_tokens"] for t in per_turn), record["total_output_tokens"]
    assert record["total_cached_tokens"] == sum(
        t["cached_tokens"] for t in per_turn), record["total_cached_tokens"]

    # The answering-turn names still describe only the turn that answered.
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50
    assert record["cached_tokens"] == 20


def test_a_one_shot_summary_reports_its_single_turn_as_the_totals(tmp_path):
    """A one-shot has one turn, so the totals equal that turn's own figures.

    A `Dispatch` carries no `total_*` of its own -- the totals only exist on the
    agentic dispatch -- so the summary must fall back to the response, not omit the
    fields a reader sums.
    """
    record = _write(tmp_path, loop.Dispatch(
        response=_reply(100, 50, 20), effort="high", attempts=1,
    ))

    assert record["total_input_tokens"] == record["input_tokens"] == 100
    assert record["total_output_tokens"] == record["output_tokens"] == 50
    assert record["total_cached_tokens"] == record["cached_tokens"] == 20
