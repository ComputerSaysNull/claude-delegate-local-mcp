"""`## Extra` records work done outside the plan; it must never be reported as the phase.

M1 once read "5 of 8 items done" while three of the five were repository tooling fixed in
passing. The backend work looked further along than it was, and STATUS.md is the file the
next session reads first to decide where it is.

Moving that work to its own section fixes the count, and introduces a quieter hazard:
`gen_status` picks the current phase as the first section holding anything unfinished, and
a new top-level section is a candidate like any other. Every item under Extra is done
today, so nothing would go wrong today -- the pointer would silently move to Extra the
first time someone records unfinished work there.

This test fails on that, rather than waiting for it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "PLAN.md"


def load_gen_status():
    """Import scripts/gen_status.py by path -- scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        "gen_status", ROOT / "scripts" / "gen_status.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plan_has_an_extra_section():
    """If this is renamed, the exclusion below stops matching and stops protecting."""
    assert "## Extra" in PLAN.read_text(encoding="utf-8"), (
        "PLAN.md has no '## Extra' heading; gen_status excludes that name by prefix, so a "
        "rename must be made in both places")


def test_extra_is_not_reported_as_the_current_phase(monkeypatch):
    """An unfinished item under Extra must not move the 'you are here' pointer."""
    gs = load_gen_status()
    real_items = gs.parse_plan()
    assert real_items, "PLAN.md yielded no items"

    # Extra must be the ONLY unfinished item, or the milestones mask it: render() takes
    # the first unfinished item it meets, so appending to a plan that already has open
    # milestones proves nothing. An earlier draft of this test did exactly that and
    # passed without the guard.
    finished = [{**i, "mark": gs.DONE} for i in real_items]
    doctored = [*finished, {"milestone": "Extra — work outside the milestone plan",
                            "mark": gs.TODO, "text": "something unfinished"}]
    monkeypatch.setattr(gs, "parse_plan", lambda: doctored)

    line = next(ln for ln in gs.render().splitlines()
                if ln.startswith("**Current phase:**"))
    assert "Extra" not in line, f"Extra became the current phase: {line}"


def test_deferred_is_not_reported_either(monkeypatch):
    """The original exclusion, asserted rather than assumed to still hold."""
    gs = load_gen_status()
    finished = [{**i, "mark": gs.DONE} for i in gs.parse_plan()]
    doctored = [*finished, {"milestone": "Deferred and cancelled",
                            "mark": gs.TODO, "text": "backlog item"}]
    monkeypatch.setattr(gs, "parse_plan", lambda: doctored)
    line = next(ln for ln in gs.render().splitlines()
                if ln.startswith("**Current phase:**"))
    assert "Deferred" not in line, f"Deferred became the current phase: {line}"


def test_a_real_milestone_is_still_selected(monkeypatch):
    """Guards the other direction: the exclusion must not swallow everything."""
    gs = load_gen_status()
    line = next(ln for ln in gs.render().splitlines()
                if ln.startswith("**Current phase:**"))
    assert "M" in line and "all planned work complete" not in line, line


def test_extra_items_still_count_in_the_totals():
    """Excluded from being the phase, not hidden. Work done should remain visible."""
    gs = load_gen_status()
    rendered = gs.render()
    assert "Extra" in rendered, "the Extra section vanished from the report entirely"


@pytest.mark.parametrize("name", ["Extra", "Deferred"])
def test_the_exclusion_is_by_prefix_on_the_heading(name):
    """Documents what the guard actually keys on, so a rename is a visible break."""
    gs = load_gen_status()
    assert name in gs.NOT_A_PHASE, (
        f"{name!r} is no longer excluded from phase selection")
