#!/usr/bin/env python3
"""Compare two endpoint captures. Structure only, never values.

    python scripts/diff_endpoint_captures.py OLD.json NEW.json

Reports fields added, removed and type-changed; metric names that appeared or
vanished; and each side's model id and date. It never prints a value, so its
output is safe to paste somewhere a capture is not -- which is the point, because
the captures live on the secret denylist and the local model therefore cannot read
them (ADR-0052). This script is what a delegation would otherwise have done.

Needs no endpoint. That is deliberate and is the half of ADR-0052 that keeps a
cluster outage from looking like a repository failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SHAPES = ("normal", "truncated", "tool_call")


def _type_of(described: str) -> str:
    """The type out of a `shape()` string, discarding whatever value follows it.

    Captures record `int = 44800` for permitted values and `NoneType (value
    withheld)` for the rest. Only the leading type is ever compared, so a value
    cannot reach this tool's output even by accident.
    """
    return described.split(" = ", 1)[0].split(" (", 1)[0].strip()


def compare_fields(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(
        f"{k}: {_type_of(old[k])} -> {_type_of(new[k])}"
        for k in set(old) & set(new)
        if _type_of(old[k]) != _type_of(new[k])
    )
    return {"added": added, "removed": removed, "type-changed": changed}


def load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"cannot read {path.name}: {type(e).__name__}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    args = ap.parse_args()

    old, new = load(args.old), load(args.new)
    for side, cap in (("old", old), ("new", new)):
        print(f"{side}: {cap.get('captured', '?')}  {cap.get('served_model_id', '?')}")

    differences = 0
    for shape in SHAPES:
        o = (old.get(shape) or {}).get("fields") or {}
        n = (new.get(shape) or {}).get("fields") or {}
        if not o and not n:
            continue
        result = compare_fields(o, n)
        if not any(result.values()):
            print(f"\n{shape}: unchanged ({len(n)} fields)")
            continue
        print(f"\n{shape}:")
        for label, items in result.items():
            for item in items:
                differences += 1
                print(f"  {label:<13} {item}")

    o_names = set((old.get("metrics") or {}).get("names") or [])
    n_names = set((new.get("metrics") or {}).get("names") or [])
    if o_names or n_names:
        appeared, vanished = sorted(n_names - o_names), sorted(o_names - n_names)
        if not appeared and not vanished:
            print(f"\nmetrics: unchanged ({len(n_names)} names)")
        else:
            print("\nmetrics:")
            for name in appeared:
                differences += 1
                print(f"  appeared      {name}")
            for name in vanished:
                differences += 1
                print(f"  vanished      {name}")

    print(f"\n{differences} structural difference(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
