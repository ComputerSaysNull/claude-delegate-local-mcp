"""Writing delegations ran out of turns at the default meant for reading passes.

Measured over every recorded stream on 2026-09-25: 26 writing delegations, a median of 27
turns used and a 90th percentile of 60, and seven of them stopped at their turn limit
while still working -- none of them looping. Every one of those handed its unfinished half
back to the caller, which is the cloud spend the server exists to avoid, while the
four-hour `dispatch_timeout` still bounds a run that goes wrong. A reading pass is bounded
by its turn cap on purpose, so it keeps the smaller default.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local import loop
from claude_delegate_local.config import Config

WRITING = frozenset({"read_file", "edit_file", "run_bash"})
READING = frozenset({"read_file", "search_files", "read_git"})


def cfg(**over) -> Config:
    return Config(workspace_roots=("/w",), **over)  # type: ignore[arg-type]


def test_a_writing_toolset_gets_the_writing_default():
    c = cfg()
    assert loop.resolve_max_turns(c, None, WRITING) == c.max_turns_default_writing
    assert c.max_turns_default_writing > c.max_turns_default


def test_a_reading_toolset_keeps_the_smaller_default():
    c = cfg()
    assert loop.resolve_max_turns(c, None, READING) == c.max_turns_default


def test_a_callers_number_still_wins_for_a_writing_toolset():
    assert loop.resolve_max_turns(cfg(), 7, WRITING) == 7


def test_a_lowered_hard_cap_lowers_the_writing_default_too():
    """Clamped rather than refused, so an operator who lowered the cap still starts."""
    assert loop.resolve_max_turns(cfg(max_turns_hard_cap=30), None, WRITING) == 30
