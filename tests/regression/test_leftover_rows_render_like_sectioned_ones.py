"""A setting must render identically wherever the generator happens to put it.

The 2026-08-28 audit found two row renderers in `gen_config_docs.py`: the sectioned loop
applied the unit suffix, the **required** marker and the **Inert.** prefix, and the
leftover "Other" loop applied none of them. So a setting's rendering depended on nothing
but whether someone had filed it under a heading.

Live symptom: two timeouts showed a bare number where every sibling said "seconds". The
latent one was worse -- the footer counts inert fields across *all* rows, so an inert
setting landing in "Other" would be counted in the total and left unmarked in its own row,
a table contradicting itself. The docs gate could not catch either: it compares the
committed file against this generator's output, and both agreed.

The fix put every field in a section, which would make a test that only reads the current
document vacuous -- "Other" is empty, so the broken loop renders nothing to check. These
tests therefore empty `SECTIONS` to force every field down the leftover path, which is the
only way to assert the second renderer is still correct rather than merely unused.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import gen_config_docs as gen  # noqa: E402


@pytest.fixture
def everything_leftover(monkeypatch):
    """Force every field down the "Other" path by leaving it nowhere else to go."""
    monkeypatch.setattr(gen, "SECTIONS", [])
    return gen.render()


def test_a_leftover_row_still_carries_its_unit(everything_leftover):
    """The live half of the bug: a bare `20.0` where its siblings said `20.0 seconds`."""
    row = _row_for(everything_leftover, "DELEGATE_RETRY_MAX_DELAY")
    assert "20.0 seconds" in row, row


def test_a_leftover_row_still_carries_its_inert_marker(monkeypatch):
    """The latent half, no longer pinned to whichever setting happens to be unbuilt.

    This asserted against a real inert field for as long as one existed: agents_dir until
    M6 read it, then kv_token_budget until M7's admission gate read that. M7 marked the
    last of them, so the specimen ran out -- and a test whose subject can go live is a
    test that goes vacuous without failing, which is the one outcome its own docstring
    said to avoid. The condition is synthesised now instead of borrowed, so the leftover
    path stays watched whether or not anything is genuinely inert.
    """
    monkeypatch.setattr(gen, "SECTIONS", [])
    monkeypatch.setattr(gen, "_unread_fields", lambda: {"kv_token_budget"})
    row = _row_for(gen.render(), "DELEGATE_KV_TOKEN_BUDGET")
    assert "**Inert.**" in row, row


def test_a_leftover_row_still_says_required(everything_leftover):
    """workspace_roots has no default; a blank cell there reads as "optional"."""
    row = _row_for(everything_leftover, "DELEGATE_WORKSPACE_ROOTS")
    assert "**required**" in row, row


@pytest.mark.parametrize("sectioned", [True, False])
def test_the_footer_count_matches_the_rows_actually_marked(monkeypatch, sectioned):
    """The self-contradiction the audit predicted, asserted in both layouts.

    Rendering the table and its total through different code is what allowed them to
    disagree, so this is the invariant that matters more than any single row. The inert
    set is forced rather than observed, for the reason above: with nothing inert the
    footer omits the clause entirely and the regex below matches nothing, which would
    read as a passing test of a format that had simply stopped being produced.
    """
    if not sectioned:
        monkeypatch.setattr(gen, "SECTIONS", [])
    monkeypatch.setattr(
        gen, "_unread_fields", lambda: {"kv_token_budget", "secret_shadow_max_depth"}
    )
    text = gen.render()
    marked = text.count("**Inert.**")
    footer = re.search(r"\*\d+ settings, (\d+) of them inert\.\*", text)
    assert footer is not None, "footer went missing; the tail format changed"
    assert marked == int(footer.group(1)), f"{marked} rows marked, footer claims {footer.group(1)}"


@pytest.mark.parametrize("sectioned", [True, False])
def test_nothing_inert_renders_no_count_and_no_marks(monkeypatch, sectioned):
    """The other side of the same invariant, and the state the project is now in.

    Zero inert fields must mean zero marked rows *and* no inert clause in the footer --
    "51 settings, 0 of them inert" would be a sentence about a thing that is not there.
    """
    if not sectioned:
        monkeypatch.setattr(gen, "SECTIONS", [])
    monkeypatch.setattr(gen, "_unread_fields", set)
    text = gen.render()
    assert "**Inert.**" not in text
    assert re.search(r"\*\d+ settings\.\*", text), "the plain footer went missing"
    assert "of them inert" not in text


def test_every_field_has_a_section_so_other_stays_empty():
    """Not the bug, but the reason the tests above have to fake their conditions.

    If this fails, a new field was added without filing it. That is not itself broken any
    more -- the leftover loop is correct now -- but "Other" carries a comment asking to be
    emptied, and an unnoticed field there is how the original divergence went unseen.
    """
    assert "### Other" not in gen.render()


def _row_for(text: str, env: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"| `{env}` |"):
            return line
    raise AssertionError(f"no row rendered for {env}")
