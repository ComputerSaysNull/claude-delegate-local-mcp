"""STATUS.md must not record where the repository happened to be when it was generated.

The bug: STATUS.md named branch `docs/repo-recreated` and commit `cdff819`. The branch had
been deleted after merge and the hash had been rewritten by the squash, so both named
something that did not exist -- and STATUS.md is a *generated* file, so the natural reaction
is to trust it.

Why nothing caught it: `gen_status.py --check` compares only the text above `## Repository`,
because counts move with every commit and a byte comparison would fail constantly. That cut
is correct, but it means nothing whatsoever verifies the text below the heading. A check that
cannot fail is worse than no check, and this is the same shape found three times before in
this repository.

So the rule is not "regenerate more often" -- it is that only facts whose staleness is
harmless may live below that heading. A count that drifts is off by one. A branch name or a
hash that drifts points at nothing. These tests hold that line.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_gen_status():
    """Import scripts/gen_status.py by path -- scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        "gen_status", ROOT / "scripts" / "gen_status.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rendered() -> str:
    return load_gen_status().render()


def unchecked_region(text: str) -> str:
    """The part of the document that `--check` cuts, and therefore never validates."""
    _, sep, tail = text.partition("## Repository")
    assert sep, "expected a '## Repository' heading to anchor the unchecked region"
    return tail


def test_no_branch_name(rendered: str) -> None:
    assert not re.search(r"(?i)\bbranch\b", rendered), (
        "STATUS.md names a branch. A branch is deleted on merge, so this is wrong the "
        "moment the change that generated it lands.")


def test_no_working_tree_state(rendered: str) -> None:
    assert not re.search(r"(?i)working tree", rendered), (
        "STATUS.md records whether the tree was dirty. That describes the instant of "
        "generation and nothing else, and it is committed, so it cannot stay true.")


def test_no_commit_hashes(rendered: str) -> None:
    found = re.findall(r"`[0-9a-f]{7,40}`", rendered)
    assert not found, (
        f"STATUS.md quotes commit hashes {found}. A squash merge rewrites them, so they "
        f"name commits that never reach the default branch.")


def test_no_recent_commits_section(rendered: str) -> None:
    assert "## Recent commits" not in rendered, (
        "A recent-commits list is a second changelog with none of the discipline -- "
        "gen_status.py's own docstring rules it out -- and `git log` is authoritative.")


def test_unchecked_region_holds_only_counts(rendered: str) -> None:
    """The real invariant, stated against the region that nothing else can verify."""
    tail = unchecked_region(rendered)
    for line in (ln.strip() for ln in tail.splitlines()):
        if not line.startswith("- "):
            continue
        # Every bullet below the heading must be counts: digits, units, punctuation.
        assert re.fullmatch(r"-(?:\s+\d[\d,]*\s+[a-z()\s]+[,.]?)+", line), (
            f"unverifiable line in STATUS.md's unchecked region: {line!r}. Only counts "
            f"belong here; anything whose staleness misleads must be left out entirely.")


def test_the_committed_file_is_clean_too() -> None:
    """Guards the guard: assertions on text nobody ships would prove nothing.

    Deliberately NOT a comparison against render() -- the counts below the heading move
    with every commit, so that test would fail constantly and get switched off, which is
    the whole reason `--check` cuts this region in the first place. Asserting the same
    shape invariant against the shipped file catches a hand-edit without the churn.
    """
    committed = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    assert not re.search(r"(?i)\bbranch\b", committed)
    assert not re.search(r"(?i)working tree", committed)
    assert not re.findall(r"`[0-9a-f]{7,40}`", committed)
    assert "## Recent commits" not in committed
