#!/usr/bin/env python3
"""Report what the dispatch ledger recorded: the facts, and any saving only as an estimate.

Written for PLAN M19.2. The ledger (`src/claude_delegate_local/ledger.py`) counts the
tokens the cluster processed, which is a fact. Calling the number a saving assumes what
Claude would otherwise have read, which is not measured -- so this reports the facts and,
only when `--saving` is asked for, states the assumption beside the figure. Reads only;
touches no cluster and no repository state.

Usage:  python scripts/ledger_report.py [--path PATH] [--by day|model|tool]
                                       [--since YYYY-MM-DD] [--saving]

`--path` defaults to the configured ledger (`ledger.path(config.load())`); when the
configuration cannot be loaded it falls back to ~/.cache/claude-delegate-local/ledger.jsonl.
The default resolves inside WSL, where the server runs.

A dispatch that failed before producing a result has null token fields. Those nulls are
never summed as zero: the dispatch is counted under "recorded no tokens" instead, so the
totals are not read as complete. A line that is not a JSON object is skipped and counted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_delegate_local import config, ledger  # noqa: E402

_DEFAULT_LEDGER = os.path.expanduser("~/.cache/claude-delegate-local/ledger.jsonl")
_GROUP_W = 24
_TOKEN_FIELDS = (
    ("total_input_tokens", "input"),
    ("total_cached_tokens", "cached"),
    ("total_output_tokens", "output"),
)


def _configured_path() -> str:
    """The ledger the server writes, or the default when the configuration cannot load."""
    try:
        cfg = config.load()
    except Exception:
        return _DEFAULT_LEDGER
    try:
        target = ledger.path(cfg)
    except Exception:
        return _DEFAULT_LEDGER
    if target is None:
        return _DEFAULT_LEDGER
    return str(target)


def _read(path: str) -> tuple[list[dict], int]:
    """Records that parsed as objects, and the count of lines that did not."""
    records: list[dict] = []
    unreadable = 0
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                unreadable += 1
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            if not isinstance(record, dict):
                unreadable += 1
                continue
            records.append(record)
    return records, unreadable


def _no_tokens(record: dict) -> bool:
    """True when the dispatch recorded no token figures, i.e. it produced no result."""
    return record.get("total_input_tokens") is None


def _day(at: object) -> str:
    """The `YYYY-MM-DD` part of an `at` timestamp, or a placeholder when absent/invalid."""
    try:
        return datetime.fromisoformat(at).date().isoformat()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "(unknown)"


def _group_key(record: dict, by: str) -> str:
    if by == "day":
        return _day(record.get("at"))
    if by == "model":
        return record.get("model_key") or "(none)"
    return record.get("tool") or "(none)"


def _in_range(record: dict, since: object) -> bool:
    """True when `--since` does not exclude the record.

    A record whose `at` is missing or unparseable is kept rather than dropped: filtering
    on a date the record does not carry is guessing, and the report would rather show it.
    """
    if since is None:
        return True
    try:
        day = datetime.fromisoformat(record["at"]).date()
    except (KeyError, TypeError, ValueError):
        return True
    return day >= since


def _aggregate(records: list[dict], by: str) -> tuple[dict[str, dict], int]:
    """Per-group counts and token sums, plus how many dispatches recorded no tokens."""
    groups: dict[str, dict] = {}
    no_tokens = 0
    for record in records:
        group = groups.setdefault(
            _group_key(record, by),
            {"dispatches": 0, "failed": 0, "input": 0, "cached": 0, "output": 0},
        )
        group["dispatches"] += 1
        if record.get("ok") is False:
            group["failed"] += 1
        if _no_tokens(record):
            no_tokens += 1
        for field, name in _TOKEN_FIELDS:
            value = record.get(field)
            if isinstance(value, int):
                group[name] += value
    return groups, no_tokens


def _total(groups: dict[str, dict]) -> dict[str, int]:
    total = {"dispatches": 0, "failed": 0, "input": 0, "cached": 0, "output": 0}
    for group in groups.values():
        for key in total:
            total[key] += group[key]
    return total


def _print_row(key: str, row: dict) -> None:
    print(f"{key:<{_GROUP_W}} {row['dispatches']:>10} {row['failed']:>7} "
          f"{row['input']:>14,} {row['cached']:>14,} {row['output']:>14,}")


def _print_table(groups: dict[str, dict]) -> None:
    print(f"{'group':<{_GROUP_W}} {'dispatches':>10} {'failed':>7} "
          f"{'input':>14} {'cached':>14} {'output':>14}")
    for key in sorted(groups):
        _print_row(key, groups[key])
    _print_row("TOTAL", _total(groups))


def _print_saving(total: dict[str, int]) -> None:
    print()
    print("An estimate, not a measurement")
    print(f"  input tokens   {total['input']:>14,}  -- what Claude would have read had it "
          "done this work itself")
    print(f"  output tokens  {total['output']:>14,}  -- what Claude would have written had "
          "it done this work itself")
    print("  Assumption: Claude would have read and written about as much as the cluster "
          "did, which is not measured.")


def report(path: str, by: str, since: object, saving: bool) -> int:
    if not os.path.isfile(path):
        print(f"ledger not found: {path}", file=sys.stderr)
        return 1
    records, unreadable = _read(path)
    records = [record for record in records if _in_range(record, since)]
    groups, no_tokens = _aggregate(records, by)
    _print_table(groups)
    if unreadable:
        print(f"{unreadable} unreadable line(s) skipped")
    if no_tokens:
        print(f"{no_tokens} dispatch(es) recorded no tokens")
    if saving:
        _print_saving(_total(groups))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--path", default="",
        help="Ledger file to report. Defaults to the configured ledger "
             "(ledger.path(config.load())); when the configuration cannot be loaded it "
             "falls back to ~/.cache/claude-delegate-local/ledger.jsonl. The default "
             "resolves inside WSL, where the server runs.",
    )
    parser.add_argument(
        "--by", choices=("day", "model", "tool"), default="day",
        help="Group by the date part of `at`, the model key, or the tool.",
    )
    parser.add_argument(
        "--since", default="",
        help="Only dispatches on or after this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--saving", action="store_true",
        help="Also print the saving as an estimate, with the assumption it rests on.",
    )
    args = parser.parse_args(argv)

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            parser.error(f"--since must be YYYY-MM-DD, got {args.since!r}")

    return report(args.path or _configured_path(), args.by, since, args.saving)


if __name__ == "__main__":
    raise SystemExit(main())
