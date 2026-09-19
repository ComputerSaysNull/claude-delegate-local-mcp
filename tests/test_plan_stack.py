"""`plan_stack`: the ordering it derives, and the promise that it only ever reads.

The ordering is the part that can be wrong without saying so. Publishing is loud — a push
prompts, a check fails, a merge is visible — but an order derived wrongly rebases one
branch onto a sibling and invents history that nobody asked for, and the first sign is a
conflict several branches later in a file nobody edited.

`is_ancestor` is injected so the whole relation can be stated in a test without a
repository. The real caller passes `git merge-base --is-ancestor`.

The read-only tests matter for a different reason. This script exists because its
predecessor published from inside a subprocess, where the operator's hook could not see
it — `PreToolUse` only ever sees a command run as a tool call. Printing the commands is
what puts each one back in front of that prompt, and a `fetch`, a `checkout` or a `push`
added here later would quietly take it away again. The refusals below are asserted rather
than trusted, because a promise about what a script does not do is worth exactly what
enforces it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "plan_stack", ROOT / "scripts" / "plan_stack.py")
plan_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_stack)


def chain(*branches: str):
    """`is_ancestor` for a straight line: each branch is an ancestor of all that follow."""
    pos = {b: i for i, b in enumerate(branches)}

    def is_ancestor(a: str, b: str) -> bool:
        return a != b and pos[a] < pos[b]

    return is_ancestor


def test_a_stack_is_ordered_base_first():
    """The fix. Shipping order is the whole point, and the input order is arbitrary."""
    order = plan_stack.order_stack(["c", "a", "b"], chain("a", "b", "c"))

    assert order == ["a", "b", "c"]


def test_an_already_ordered_stack_is_unchanged():
    """Control. A correct input must not be reshuffled into a different correct-looking one."""
    order = plan_stack.order_stack(["a", "b", "c"], chain("a", "b", "c"))

    assert order == ["a", "b", "c"]


def test_a_single_branch_is_a_stack():
    """Control, and the commonest real case."""
    assert plan_stack.order_stack(["only"], chain("only")) == ["only"]


def test_two_branches_off_main_are_refused():
    """The negative control, and the reason this function exists rather than a sort.

    Neither is an ancestor of the other, so there is no order. A sort would still return
    one, and the caller would rebase a branch onto an unrelated sibling.
    """
    def unrelated(a: str, b: str) -> bool:
        return False

    with pytest.raises(SystemExit, match=r"not a stack|not in one chain"):
        plan_stack.order_stack(["a", "b"], unrelated)


def test_a_fork_half_way_up_is_refused():
    """The shape that looks like a stack until the last pair: a and b, then b forks."""
    pos = {"a": 0, "b": 1, "c": 2, "d": 2}

    def forked(x: str, y: str) -> bool:
        # c and d both descend from b, and neither from the other.
        return x != y and pos[x] < pos[y]

    with pytest.raises(SystemExit):
        plan_stack.order_stack(["a", "b", "c", "d"], forked)


def test_the_refusal_names_both_branches():
    """A refusal that does not say which pair broke leaves the reader to re-derive it."""
    def unrelated(a: str, b: str) -> bool:
        return False

    with pytest.raises(SystemExit) as e:
        plan_stack.order_stack(["alpha", "beta"], unrelated)

    assert "alpha" in str(e.value) and "beta" in str(e.value)


def test_a_claimed_number_is_read_from_the_newest_heading(monkeypatch):
    """The guard that caught a real mismatch: the heading is compared to what GitHub issues.

    The first draft of this parser split on `#` and returned an empty string for every
    heading, so the comparison passed nothing against a real number and would have let a
    wrong heading reach `main`.
    """
    monkeypatch.setattr(plan_stack, "git", lambda *a, **k: (
        "# Changelog\n\n## #239 — 2026-09-18 — feat: a thing\n\n## #238 — older\n"))

    assert plan_stack.claimed_number("any-branch") == "239"


def test_a_changelog_with_no_heading_reads_as_none(monkeypatch):
    """Control. None is what makes the caller refuse rather than compare against junk."""
    monkeypatch.setattr(plan_stack, "git", lambda *a, **k: "# Changelog\n\nNo entries.\n")

    assert plan_stack.claimed_number("any-branch") is None


# --- the read-only promise, which is the reason this script replaced a publisher ---------

@pytest.mark.parametrize("subcommand", ["fetch", "checkout", "reset", "rebase", "push"])
def test_a_git_subcommand_that_moves_something_is_refused(subcommand, monkeypatch):
    """The negative control, one per verb the predecessor used to run itself."""
    monkeypatch.setattr(plan_stack, "run", lambda *a, **k: "")

    with pytest.raises(SystemExit, match="plans and does not publish"):
        plan_stack.git(subcommand, "whatever")


def test_the_reads_it_needs_are_allowed(monkeypatch):
    """Control. A refusal that caught everything would be a script that does nothing."""
    seen = []
    monkeypatch.setattr(plan_stack, "run", lambda cmd, **k: seen.append(cmd) or "")

    for subcommand in sorted(plan_stack.READ_ONLY_GIT):
        plan_stack.git(subcommand)

    assert [c[1] for c in seen] == sorted(plan_stack.READ_ONLY_GIT)


@pytest.mark.parametrize("args", [("pr", "create"), ("pr", "merge"), ("pr", "edit")])
def test_a_gh_call_that_publishes_is_refused(args, monkeypatch):
    monkeypatch.setattr(plan_stack, "run", lambda *a, **k: "")

    with pytest.raises(SystemExit, match="anything that publishes"):
        plan_stack.gh(*args)


@pytest.mark.parametrize("what", ["pr", "issue"])
def test_listing_is_allowed_for_both_counters(what, monkeypatch):
    """Control. Both, because pull requests and issues draw from one number counter.

    Asserted per counter because the first draft allowed only `pr list`, and the refusal
    then fired on the script's own number check — caught by running it, which is what a
    control that only tested the allowed case would have missed.
    """
    monkeypatch.setattr(plan_stack, "run", lambda *a, **k: "241")

    assert plan_stack.gh(what, "list", "--state", "all") == "241"
