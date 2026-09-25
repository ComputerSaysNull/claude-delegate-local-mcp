"""`ledger_report`: the ledger counts cluster tokens, and any saving is an estimate.

PLAN M19.2. The ledger is a fact; calling its count a saving assumes what Claude would
otherwise have read, which is not measured. The report prints the facts and, only under
`--saving`, states that assumption beside the figure. These tests build ledgers in
`tmp_path` and drive the script's `main` with an explicit `--path`, so no configuration is
touched.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ledger_report.py"


@pytest.fixture(scope="module")
def ledger_report():
    spec = importlib.util.spec_from_file_location("ledger_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EPOCH = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _iso(days: int) -> str:
    """An ISO timestamp `days` after a fixed epoch, for the `at` field."""
    return (_EPOCH + timedelta(days=days)).isoformat()


def _dispatch(**kw: object) -> dict:
    """One ledger line, complete enough to be a real record, with the fields overridden."""
    record: dict = {
        "at": _iso(0),
        "tool": "delegate",
        "agent": "ops",
        "model_key": "deepseek-v3",
        "served_model_id": "deepseek",
        "effort": "low",
        "ok": True,
        "error": None,
        "turns": 3,
        "elapsed_seconds": 12.0,
        "total_input_tokens": 1000,
        "total_output_tokens": 200,
        "total_cached_tokens": 500,
    }
    record.update(kw)
    return record


def _write(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in records)


def _rows(out: str) -> dict[str, dict[str, int]]:
    """The table rows keyed by their group column, with commas stripped."""
    rows: dict[str, dict[str, int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 6 or parts[0] == "group":
            continue
        rows[parts[0]] = {
            "dispatches": int(parts[1]),
            "failed": int(parts[2]),
            "input": int(parts[3].replace(",", "")),
            "cached": int(parts[4].replace(",", "")),
            "output": int(parts[5].replace(",", "")),
        }
    return rows


def test_the_totals_over_three_dispatches(ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, [
        _dispatch(model_key="m1", total_input_tokens=100,
                  total_cached_tokens=50, total_output_tokens=10),
        _dispatch(model_key="m1", total_input_tokens=200,
                  total_cached_tokens=100, total_output_tokens=20),
        _dispatch(model_key="m2", total_input_tokens=300,
                  total_cached_tokens=150, total_output_tokens=30),
    ])

    assert ledger_report.main(argv=["--path", str(path)]) == 0
    out = capsys.readouterr().out

    total = _rows(out)["TOTAL"]
    assert total["dispatches"] == 3
    assert total["failed"] == 0
    assert total["input"] == 600
    assert total["cached"] == 300
    assert total["output"] == 60


def test_a_null_token_dispatch_is_counted_and_not_summed(
        ledger_report, tmp_path, capsys) -> None:
    """A failed dispatch's null tokens are not summed as zero; it is counted separately."""
    path = tmp_path / "ledger.jsonl"
    _write(path, [
        _dispatch(total_input_tokens=100, total_cached_tokens=50, total_output_tokens=10),
        _dispatch(ok=False, total_input_tokens=None,
                  total_cached_tokens=None, total_output_tokens=None),
    ])

    assert ledger_report.main(argv=["--path", str(path)]) == 0
    out = capsys.readouterr().out

    assert "1 dispatch(es) recorded no tokens" in out
    total = _rows(out)["TOTAL"]
    assert total["dispatches"] == 2
    assert total["failed"] == 1
    assert total["input"] == 100
    assert total["cached"] == 50
    assert total["output"] == 10


def test_a_malformed_line_is_skipped_and_counted(ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps(_dispatch(total_input_tokens=100, total_cached_tokens=50,
                             total_output_tokens=10))
        + "\nnot json at all\n[1, 2, 3]\n"
        + json.dumps(_dispatch(total_input_tokens=50, total_cached_tokens=25,
                               total_output_tokens=5))
        + "\n",
        encoding="utf-8",
    )

    assert ledger_report.main(argv=["--path", str(path)]) == 0
    out = capsys.readouterr().out

    assert "2 unreadable line(s) skipped" in out
    total = _rows(out)["TOTAL"]
    assert total["dispatches"] == 2
    assert total["input"] == 150


def test_grouping_by_model(ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, [
        _dispatch(model_key="m1", total_input_tokens=100,
                  total_cached_tokens=50, total_output_tokens=10),
        _dispatch(model_key="m1", total_input_tokens=200,
                  total_cached_tokens=100, total_output_tokens=20),
        _dispatch(model_key="m2", total_input_tokens=300,
                  total_cached_tokens=150, total_output_tokens=30),
    ])

    assert ledger_report.main(argv=["--path", str(path), "--by", "model"]) == 0
    out = capsys.readouterr().out

    rows = _rows(out)
    assert rows["m1"]["dispatches"] == 2
    assert rows["m1"]["input"] == 300
    assert rows["m2"]["dispatches"] == 1
    assert rows["m2"]["input"] == 300
    assert rows["TOTAL"]["dispatches"] == 3


def test_since_filters_by_date(ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, [
        _dispatch(at=_iso(0), total_input_tokens=100,
                  total_cached_tokens=50, total_output_tokens=10),
        _dispatch(at=_iso(5), total_input_tokens=200,
                  total_cached_tokens=100, total_output_tokens=20),
        _dispatch(at=_iso(10), total_input_tokens=300,
                  total_cached_tokens=150, total_output_tokens=30),
    ])
    since = (_EPOCH + timedelta(days=5)).date().isoformat()

    assert ledger_report.main(argv=["--path", str(path), "--since", since]) == 0
    out = capsys.readouterr().out

    total = _rows(out)["TOTAL"]
    assert total["dispatches"] == 2
    assert total["input"] == 500
    assert total["cached"] == 250
    assert total["output"] == 50


def test_saving_states_the_assumption_beside_the_estimate(
        ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, [_dispatch(total_input_tokens=100, total_cached_tokens=50,
                            total_output_tokens=10)])

    assert ledger_report.main(argv=["--path", str(path), "--saving"]) == 0
    out = capsys.readouterr().out

    assert "An estimate, not a measurement" in out
    assert "what Claude would have read" in out
    assert "what Claude would have written" in out
    assert "would have read and written about as much as the cluster did" in out
    assert "which is not measured" in out


def test_without_saving_neither_the_estimate_nor_the_assumption_appears(
        ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, [_dispatch(total_input_tokens=100, total_cached_tokens=50,
                            total_output_tokens=10)])

    assert ledger_report.main(argv=["--path", str(path)]) == 0
    out = capsys.readouterr().out

    assert "An estimate, not a measurement" not in out
    assert "would have read and written" not in out
    assert "which is not measured" not in out


def test_a_missing_file_exits_one(ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "does-not-exist.jsonl"

    assert ledger_report.main(argv=["--path", str(path)]) == 1
    captured = capsys.readouterr()

    assert "ledger not found" in captured.err


def test_an_empty_file_prints_the_empty_table(ledger_report, tmp_path, capsys) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text("", encoding="utf-8")

    assert ledger_report.main(argv=["--path", str(path)]) == 0
    out = capsys.readouterr().out

    total = _rows(out)["TOTAL"]
    assert total["dispatches"] == 0
    assert total["input"] == 0
