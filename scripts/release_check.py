#!/usr/bin/env python3
"""Refuse a release tag that does not name the version `pyproject.toml` carries.

Run by the release workflow before it builds anything (ADR-0115). A tag and a version that
disagree would publish a package whose version is not its own name, and a reader pinning
the transcript schema to that release would pin something other than what it asked for.

    python scripts/release_check.py v0.6.0      # exit 0 when pyproject says 0.6.0
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["version"]
    tag = argv[1] if len(argv) > 1 else ""
    if tag == f"v{version}":
        print(f"release tag {tag} names version {version}")
        return 0
    print(f"refused: the tag {tag!r} does not name the package version {version}. "
          f"Tag v{version}, or change the version in pyproject.toml first.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
