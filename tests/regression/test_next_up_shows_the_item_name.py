"""STATUS.md's queued lists show an item's name, not its first physical line.

`parse_plan` reads one line per item, so an item's `text` is whatever fitted on the line
carrying its marker. PLAN.md wraps annotations over several lines, so rendering `text`
whole cuts the sentence wherever the wrap fell: "Operator allowlist for an agent's
`network` and `extra_binds` -- the only validation is".

That was latent for as long as it was unreachable. `In progress` has never had an entry,
and `Next up` reads from the current phase, which had nothing queued from the moment M7
closed until the plan's backlog was given a section of its own. The first real queue put
three sentences cut mid-clause into a generated file README.md points a new reader at.

The two tests below are the pair that matters: one proves the name is recovered, and one
proves the raw text it replaced was actually broken -- without the second, the first would
pass just as happily against the bug.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WRAPPED = ("Operator allowlist for an agent's `network` and `extra_binds` — the only "
           "validation is")


def load_gen_status():
    """Import scripts/gen_status.py by path -- scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        "gen_status", ROOT / "scripts" / "gen_status.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_annotation_is_dropped_and_the_name_kept():
    gs = load_gen_status()
    assert gs.name_of(WRAPPED) == (
        "Operator allowlist for an agent's `network` and `extra_binds`")


def test_the_text_it_replaced_really_was_cut_mid_clause():
    """The negative half. A name-recovering function that never had to recover anything
    would pass the test above unchanged, so assert the input is genuinely broken."""
    assert WRAPPED.endswith("the only validation is"), (
        "this fixture no longer reproduces a wrapped line, so the test above proves nothing")
    assert gs_name(WRAPPED) != WRAPPED, "name_of returned its input; nothing was trimmed"


def gs_name(text: str) -> str:
    return load_gen_status().name_of(text)


def test_an_item_with_no_annotation_is_left_alone():
    """Splitting must not eat an item that is only a name."""
    assert gs_name("Anthropic-compatible adapter") == "Anthropic-compatible adapter"


def test_a_dash_at_the_end_of_the_line_is_handled():
    """PLAN.md wraps straight after the separator often enough to matter: the line ends
    "... on the dispatch deadline —", with no space after the dash to split on."""
    assert gs_name(
        "Say in `docs/DISPATCH.md` that the admission wait stacks on the dispatch deadline —"
    ) == "Say in `docs/DISPATCH.md` that the admission wait stacks on the dispatch deadline"


def test_the_rendered_queue_carries_no_cut_clause():
    """End to end against the live PLAN.md: every queued line is a name."""
    gs = load_gen_status()
    lines = gs.render().splitlines()
    start = lines.index("## Next up")
    queued = [ln[2:] for ln in lines[start + 2:] if ln.startswith("- ")]
    assert queued, "nothing queued; this test needs a phase with open items to mean anything"
    for name in queued:
        assert "—" not in name, f"queued line still carries its annotation: {name!r}"
