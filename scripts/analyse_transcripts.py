#!/usr/bin/env python3
"""Read the operator transcripts and report what delegations actually cost.

Written 2026-09-05 for the two findings in JOURNAL 2026-09-05 ("The turn loop throws away
the prefix cache") and kept so the fixes can be checked against the same instrument that
found the bugs. Reads only; touches no cluster and no repository state.

Three reports, in the order they were needed:

  rates        Fits `backend_seconds ~ prefill/P + output/D` over every turn that reports a
               cache hit, giving measured prefill and decode rates. Two unknowns and one
               equation per turn, solved by ordinary least squares through the origin. The
               residual is printed because a two-parameter model of a serving stack is an
               approximation, and one that stopped fitting would be the interesting result.

  cache        Per-turn prompt against per-turn cache hit, one block per delegation. This
               is where the eviction collapse is visible: the hit rate climbs while the
               history is appended to, then falls to a floor on the turn the first tool
               result is evicted and creeps back one stub at a time.

  concurrency  Aggregate against per-sequence throughput. Each turn's output is spread over
               its own backend interval, the timeline is sampled on a one-second grid, and
               each instant is bucketed by how many delegations were decoding then. The
               aggregate column is the cluster's; the per-sequence column is what a single
               delegation's deadline is measured against, and they move in opposite
               directions -- which is the whole point of running it.

Usage:  python scripts/analyse_transcripts.py [rates|cache|concurrency|all] [--dir PATH]

The directory defaults to DELEGATE_TRANSCRIPT_DIR, so a checkout with a configured server
needs no argument. Nothing here is imported by the server.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime

STEP_SECONDS = 1.0


def _load_dotenv_value(key: str) -> str:
    """DELEGATE_TRANSCRIPT_DIR from the environment, falling back to a local .env.

    Deliberately not `config.load()`: this script must run against a checkout whose server
    will not start, which is exactly the state a bad change leaves behind.
    """
    if os.environ.get(key):
        return os.environ[key]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _turns(directory: str) -> list[dict]:
    """Every `turn` record, tagged with the stream it came from."""
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("t") == "turn":
                    record["_stream"] = name
                    out.append(record)
    return out


def _streams(directory: str) -> dict[str, tuple[float, float]]:
    """First and last event time per stream, for the concurrency timeline."""
    spans: dict[str, tuple[float, float]] = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
        stamps: list[float] = []
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                at = json.loads(line).get("at")
                if at:
                    stamps.append(datetime.fromisoformat(at).timestamp())
        if stamps:
            spans[os.path.basename(path)] = (min(stamps), max(stamps))
    return spans


def _fit(rows: list[tuple[float, float, float]]) -> tuple[float, float]:
    """Least squares through the origin for `seconds = a*prefill + b*output`."""
    sxx = sum(p * p for p, _, _ in rows)
    syy = sum(o * o for _, o, _ in rows)
    sxy = sum(p * o for p, o, _ in rows)
    sxs = sum(p * s for p, _, s in rows)
    sys_ = sum(o * s for _, o, s in rows)
    det = sxx * syy - sxy * sxy
    if not det:
        raise SystemExit("not enough variation in the records to fit two rates")
    return (sxs * syy - sys_ * sxy) / det, (sys_ * sxx - sxs * sxy) / det


def _priced(records: list[dict]) -> list[tuple[float, float, float]]:
    rows = []
    for r in records:
        if r.get("cached_tokens") is None or r.get("attempts", 1) != 1:
            continue
        seconds = (r.get("backend_ms") or 0) / 1000.0
        if seconds <= 0:
            continue
        uncached = max((r.get("input_tokens") or 0) - r["cached_tokens"], 0)
        rows.append((float(uncached), float(r.get("output_tokens") or 0), seconds))
    return rows


def report_rates(directory: str) -> None:
    rows = _priced(_turns(directory))
    if len(rows) < 4:
        raise SystemExit(f"only {len(rows)} priced turns; need at least 4 to fit")
    a, b = _fit(rows)
    residuals = [abs(a * p + b * o - s) for p, o, s in rows]
    mean_turn = sum(s for _, _, s in rows) / len(rows)
    mae = sum(residuals) / len(residuals)
    print(f"turns fitted            {len(rows)}")
    print(f"prefill rate            {1 / a:,.0f} tok/s")
    print(f"decode rate             {1 / b:,.1f} tok/s")
    print(f"mean turn               {mean_turn:,.1f} s")
    print(f"mean abs residual       {mae:,.1f} s  ({100 * mae / mean_turn:.0f}% of a turn)")
    print()
    print("What a reply budget costs at that decode rate:")
    for budget in (8_192, 32_768, 65_536, 131_072):
        print(f"  max_tokens {budget:>7,}  ->  {budget * b:>7,.0f} s  ({budget * b / 60:.1f} min)")
    print()
    print("What a deadline can return at that decode rate:")
    for name, seconds in (("turn_timeout 1800", 1800), ("stall_timeout 2100", 2100)):
        print(f"  {name}s  ->  {seconds / b:>8,.0f} output tokens")


def report_cache(directory: str) -> None:
    by_stream: dict[str, list[dict]] = {}
    for record in _turns(directory):
        by_stream.setdefault(record["_stream"], []).append(record)
    for stream, records in sorted(by_stream.items()):
        priced = [r for r in records if r.get("cached_tokens") is not None]
        if len(priced) < 3:
            continue
        print("=" * 76)
        print(stream)
        # No eviction column, deliberately. `Stream.turn` does not write
        # `tool_results_evicted` -- only the final record does, per turn -- so a column for
        # it here could never populate from a live stream, and a column that cannot fill is
        # the display form of a check that cannot fail. Read the collapse off `hit%` and
        # `backend_s` instead: the turn where one falls and the other jumps is the one.
        print("  turn      prompt      cached    hit%   backend_s")
        for r in priced:
            prompt = r.get("input_tokens") or 0
            cached = r["cached_tokens"]
            hit = (100.0 * cached / prompt) if prompt else 0.0
            seconds = (r.get("backend_ms") or 0) / 1000.0
            print(f"  {r.get('turn', 0):>4}  {prompt:>10,}  {cached:>10,}  "
                  f"{hit:>6.1f}  {seconds:>10.1f}")


def report_concurrency(directory: str) -> None:
    spans = _streams(directory)
    intervals = []
    for r in _turns(directory):
        seconds = (r.get("backend_ms") or 0) / 1000.0
        at = r.get("at")
        if seconds <= 0 or not at:
            continue
        finish = datetime.fromisoformat(at).timestamp()
        output = r.get("output_tokens") or 0
        intervals.append((finish - seconds, finish, output / seconds))
    if not intervals:
        raise SystemExit("no priced turns found")

    tokens: dict[int, float] = {}
    seconds: dict[int, float] = {}
    now, end = min(i[0] for i in intervals), max(i[1] for i in intervals)
    while now < end:
        live = [rate for start, stop, rate in intervals if start <= now < stop]
        if live:
            tokens[len(live)] = tokens.get(len(live), 0.0) + sum(live) * STEP_SECONDS
            seconds[len(live)] = seconds.get(len(live), 0.0) + STEP_SECONDS
        now += STEP_SECONDS

    print(f"{len(spans)} streams, {len(intervals)} priced turns")
    print()
    print("decoding    wall s      tokens   aggregate   per sequence")
    for count in sorted(tokens):
        aggregate = tokens[count] / seconds[count]
        print(f"  {count:>5}  {seconds[count]:>9,.0f}  {tokens[count]:>10,.0f}  "
              f"{aggregate:>10.1f}  {aggregate / count:>13.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", nargs="?", default="all",
                        choices=("rates", "cache", "concurrency", "all"))
    parser.add_argument("--dir", default="", help="transcript directory")
    args = parser.parse_args()

    directory = args.dir or _load_dotenv_value("DELEGATE_TRANSCRIPT_DIR")
    if not directory:
        print("No transcript directory. Set DELEGATE_TRANSCRIPT_DIR or pass --dir.",
              file=sys.stderr)
        return 2
    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        print(f"Not a directory: {directory}", file=sys.stderr)
        if directory.startswith("/") and os.name == "nt":
            # `wsl.py` translates Windows paths to POSIX and not the reverse, because the
            # server only ever needs that direction, and CLAUDE.md keeps the boundary in
            # that one module. So this says where to run rather than translating here.
            print("That is a POSIX path and this is Windows. Either run it on the side "
                  "that can see the directory:\n"
                  "  wsl -d Ubuntu-24.04 -e bash -lc "
                  "'python3 scripts/analyse_transcripts.py'\n"
                  "or pass the Windows form with --dir.", file=sys.stderr)
        return 2

    reports = ("rates", "cache", "concurrency") if args.report == "all" else (args.report,)
    for index, name in enumerate(reports):
        if index:
            print("\n")
        print(f"--- {name} ---\n")
        {"rates": report_rates, "cache": report_cache,
         "concurrency": report_concurrency}[name](directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
