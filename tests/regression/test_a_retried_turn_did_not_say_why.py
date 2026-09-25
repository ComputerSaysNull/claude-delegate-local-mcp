"""A retried turn did not say why.

`attempts` counts a dropped connection or a 429/5xx, but the error was caught and slept on
unrecorded, so a turn with two attempts could not be diagnosed from the stream, the summary
or the log. The kind, the HTTP status and the seconds each failed attempt spent were all in
scope in `complete_with_retry`'s `except` and all discarded.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from test_loop import ScriptedBackend, SleepSpy, cfg, entry, ok_response, one_shot

from claude_delegate_local import loop, transcript
from claude_delegate_local.backends.base import BackendRefused, BackendUnavailable


# --- what `complete_with_retry` reports for each failed attempt --------------------------


def test_a_dropped_connection_reports_its_kind():
    """A connect failure is caught and slept on, and the record must say it was one."""
    records = []
    backend = ScriptedBackend([BackendUnavailable("route dropped"), ok_response("second try")])
    _, attempts, _ = asyncio.run(
        loop.complete_with_retry(
            cfg(retry_max_attempts=3), backend, one_shot("hello"),
            on_retry=records.append, sleep=SleepSpy(),
        )
    )
    assert attempts == 2
    assert len(records) == 1
    assert records[0].kind == "BackendUnavailable"
    assert records[0].status is None


def test_a_refusal_reports_its_http_status():
    """429/5xx are retried, and which one the endpoint sent is the fact to keep."""
    records = []
    backend = ScriptedBackend(
        [BackendRefused(503, "later", "/v1/chat/completions"), ok_response()]
    )
    _, attempts, _ = asyncio.run(
        loop.complete_with_retry(
            cfg(retry_max_attempts=2), backend, one_shot("hello"),
            on_retry=records.append, sleep=SleepSpy(),
        )
    )
    assert attempts == 2
    assert len(records) == 1
    assert records[0].kind == "BackendRefused"
    assert records[0].status == 503


def test_the_record_carries_the_wait_it_chose():
    """The sleep is part of the diagnosis: a long wait explains why the turn took so long."""
    records = []
    backend = ScriptedBackend(
        [BackendRefused(429, "slow down", "/v1/chat/completions", "7"), ok_response()]
    )
    asyncio.run(
        loop.complete_with_retry(
            cfg(retry_max_attempts=3, retry_base_delay=1.0, retry_max_delay=60.0),
            backend,
            one_shot("hello"),
            on_retry=records.append,
            sleep=SleepSpy(),
        )
    )
    assert records[0].wait == 7.0


# --- the turn stream event ---------------------------------------------------------------


def _turn_event(path: Path, retries) -> dict:
    """One turn event for a diagnostic with the given retries, written then read back."""
    diag = loop.TurnDiagnostic(
        turn=1,
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        attempts=2 if retries else 1,
        effort="low",
        evicted=0,
        tool_calls=(),
        retries=retries,
    )
    transcript.Stream(path).turn(diag, "an answer", ms=1000, backend_ms=900, of_turns=1)
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def test_a_turn_with_a_retry_streams_it(tmp_path):
    """The field that makes a two-attempt turn explainable while it is being watched."""
    event = _turn_event(
        tmp_path / "one.jsonl",
        (loop.RetryRecord(kind="BackendUnavailable", status=None, seconds=1.234, wait=2.0),),
    )
    assert event["retries"] == [
        {"kind": "BackendUnavailable", "status": None, "seconds": 1.234, "wait": 2.0}
    ]


def test_a_turn_with_no_retry_has_no_retries_key(tmp_path):
    """Absent, not an empty list: `[]` would read as "nothing retried", which is a claim
    only the absence can make without pretending to know how the attempt was won."""
    event = _turn_event(tmp_path / "none.jsonl", ())
    assert "retries" not in event


# --- the recovery ladder carries the record out -----------------------------------------


def test_the_recovery_ladder_fills_dispatch_retries():
    """`Dispatch.retries` is the field the turn loop and the summary read, so it has to be
    filled by the ladder that owns the retries rather than left empty."""
    backend = ScriptedBackend([BackendUnavailable("dropped"), ok_response("answered")])
    dispatch = asyncio.run(
        loop.dispatch_with_recovery(
            cfg(retry_max_attempts=3),
            entry(),
            backend,
            lambda level, budget: loop.build_one_shot_request(
                delegation=loop.Delegation("x"), effort=level,
                max_tokens=budget, temperature=1.0, top_p=1.0,
            ),
            effort="low",
            deadline=None,
            sleep=SleepSpy(),
        )
    )
    assert dispatch.response.text == "answered"
    assert len(dispatch.retries) == 1
    assert dispatch.retries[0].kind == "BackendUnavailable"
    assert dispatch.retries[0].status is None
