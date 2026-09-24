"""Every suite run appended fake dispatches to the operator's real token ledger.

Configs built in tests carry the default `ledger_path`, which is under the real home, so each
server-level test that dispatched wrote a line there: 253 in WSL, 419 on Windows and 142 in
the sandbox's home after one evening, all of them fake, in the file the cost report sums.
`conftest.py` now redirects the default for the whole session; a test naming its own path
keeps it.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_delegate_local import ledger
from claude_delegate_local.config import Config

REAL = Path(os.path.expanduser("~/.cache/claude-delegate-local/ledger.jsonl"))


def test_a_default_config_does_not_reach_the_real_ledger():
    target = ledger.path(Config(workspace_roots=(".",)))
    assert target is not None
    assert target.resolve() != REAL.resolve(), f"a test would append to {REAL}"


def test_a_path_a_test_names_is_left_alone(tmp_path: Path):
    """The negative control: the redirect must not swallow a path a test chose."""
    chosen = tmp_path / "mine.jsonl"
    assert ledger.path(Config(workspace_roots=(".",), ledger_path=str(chosen))) == chosen
