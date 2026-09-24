"""`analyse_transcripts.py concurrency` models a turn over its decode window, not `backend_ms`.

PLAN M16.10. The report used to spread a turn's tokens over `backend_ms` -- which also
spans prefill and the admission queue -- so the shared-bucket aggregate came out under the
cluster's real decode throughput. A `t: "turn"` event already carries `decode_seconds` and
`out_tok_s` (written by `Stream.turn`), so the report now models each turn as the interval
`(finish - decode_seconds, finish)` at `out_tok_s`, and skips events from older streams that
lack either, reporting how many it skipped.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyse_transcripts.py"


@pytest.fixture(scope="module")
def analyse():
    spec = importlib.util.spec_from_file_location("analyse_transcripts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EPOCH = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _iso(seconds: float) -> str:
    """An ISO timestamp `seconds` after a fixed epoch, for the `at` field."""
    return (_EPOCH + timedelta(seconds=seconds)).isoformat()


def _bucket_aggregate(out: str, count: int) -> float:
    """The `aggregate` column of the row whose `decoding` bucket is `count`."""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == str(count):
            return float(parts[3])
    raise AssertionError(f"no decoding bucket for {count} in output:\n{out}")


def test_concurrency_spans_the_decode_window_not_backend(analyse, tmp_path, capsys):
    """Two turns whose decode windows overlap, each with a long prefill.

    `backend_ms` is several times `decode_seconds`, so the old model diluted the rate
    (output / backend_ms) and widened the interval out across prefill. The shared bucket's
    aggregate should be the sum of the two `out_tok_s`, not something diluted by prefill.
    """
    events = [
        # Turn 1: 5 s of decode, 5 s of prefill, backend_ms 10 s.
        {
            "t": "turn", "at": _iso(10.0), "turn": 1,
            "backend_ms": 10_000, "decode_seconds": 5.0, "prefill_seconds": 5.0,
            "output_tokens": 50, "out_tok_s": 10.0, "attempts": 1,
        },
        # Turn 2: 6 s of decode, 6 s of prefill, backend_ms 12 s. Its decode window
        # (6..12) overlaps turn 1's (5..10) for four whole one-second samples.
        {
            "t": "turn", "at": _iso(12.0), "turn": 2,
            "backend_ms": 12_000, "decode_seconds": 6.0, "prefill_seconds": 6.0,
            "output_tokens": 48, "out_tok_s": 8.0, "attempts": 1,
        },
        # An older-stream event with no `decode_seconds` must be skipped, not guessed at.
        {
            "t": "turn", "at": _iso(20.0), "turn": 3,
            "backend_ms": 5_000, "prefill_seconds": 5.0,
            "output_tokens": 40, "out_tok_s": 8.0, "attempts": 1,
        },
    ]
    stream = tmp_path / "one-stream.jsonl"
    with open(stream, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(event) + "\n" for event in events)

    analyse.report_concurrency(str(tmp_path))
    out = capsys.readouterr().out

    aggregate = _bucket_aggregate(out, 2)
    assert aggregate == pytest.approx(10.0 + 8.0, abs=2.0)
    assert "skipped 1" in out
