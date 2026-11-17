#!/usr/bin/env python3
"""Report the dispatch ledger from a checkout: a wrapper around the package module.

`claude-delegate-local-mcp report` is the installed form, dispatched by `main.run`. This
script exists so a checkout can print the same report without the console script on PATH.
The logic and the flags live in `src/claude_delegate_local/ledger_report.py`; this file only
puts `src/` on the path and calls its `main`, so the two cannot drift.

Usage:  python scripts/ledger_report.py [--path PATH] [--by day|model|tool]
                                       [--since YYYY-MM-DD] [--saving]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_delegate_local import ledger_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """The package's `main`, so `python scripts/ledger_report.py` and the subcommand agree."""
    return ledger_report.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
