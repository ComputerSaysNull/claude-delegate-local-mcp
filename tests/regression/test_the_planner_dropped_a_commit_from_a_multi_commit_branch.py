"""The stack planner dropped commits from a branch that had more than one.

Every restack line it printed was `git rebase --onto <parent> <old tip>^ <branch>`, which
is right only when a branch is one commit: `<tip>^` is then its parent's tip. On
2026-09-23 the front branch of a real stack had two commits, and the printed line would
have replayed only the second -- the first was dropped silently, and only a CHANGELOG
conflict gave it away. A child's base is its parent's old tip, whatever it holds.

The front branch has no parent left to name once that parent is squash-merged and
deleted. Its own commits begin after the newest ancestor whose tree `main` now holds,
which is the squashed parent; with nothing merged yet, they begin at the fork point.

The title had the same assumption. It was the tip commit's subject, which for a branch of
several commits is not the pull request's title -- that is the CHANGELOG heading's.

Driven against a real repository, and the printed rebases are then run, so the test
checks the outcome rather than the wording.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("plan_stack", _ROOT / "scripts" / "plan_stack.py")
plan_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_stack)


def _git(repo: Path, *args: str) -> str:
    env_args = ["-c", "user.name=Test", "-c", "user.email=t@example.com",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    done = subprocess.run(["git", *env_args, *args], cwd=repo, check=True,
                          capture_output=True, text=True, encoding="utf-8")
    return done.stdout.strip()


def _commit(repo: Path, name: str, heading: str | None = None) -> str:
    (repo / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
    if heading is not None:
        log = repo / "CHANGELOG.md"
        old = log.read_text(encoding="utf-8")
        log.write_text(old.replace("# Changelog\n", f"# Changelog\n\n{heading}\n\n"
                                   f"### Fixed\n\n- **{name}.**\n", 1), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"fix: {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    _commit(r, "m0")
    monkeypatch.setattr(plan_stack, "ROOT", r)
    monkeypatch.setattr(plan_stack, "gh", lambda *a, **k: "")  # number check skipped
    monkeypatch.setattr(plan_stack, "write_temp", lambda text, suffix: f"/tmp/x{suffix}")
    return r


def _two_commit_stack(repo: Path) -> dict[str, str]:
    _git(repo, "checkout", "-q", "-b", "a")
    a1 = _commit(repo, "a1", "## #10 — 2026-09-23 — fix: the heading of a")
    a2 = _commit(repo, "a2")
    _git(repo, "checkout", "-q", "-b", "b")
    b1 = _commit(repo, "b1", "## #11 — 2026-09-23 — feat: the heading of b")
    b2 = _commit(repo, "b2")
    return {"a1": a1, "a2": a2, "b1": b1, "b2": b2}


def _rebases(out: str) -> list[list[str]]:
    return [line.split()[1:] for line in out.splitlines() if line.strip().startswith("git rebase")]


def test_a_multi_commit_child_rebases_from_its_parents_old_tip(repo, capsys):
    tips = _two_commit_stack(repo)
    assert plan_stack.plan(["a", "b"], verification="- ok") == 0
    lines = _rebases(capsys.readouterr().out)
    assert ["rebase", "--onto", "a", tips["a2"], "b"] in lines, lines


def test_a_multi_commit_front_keeps_every_commit_after_its_parent_was_squashed(repo, capsys):
    tips = _two_commit_stack(repo)
    # What `gh pr merge --squash --delete-branch` leaves behind: one commit on main with
    # a's tree, and no branch named a.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "a")
    _git(repo, "commit", "-q", "-m", "fix: the heading of a (#10)")
    _git(repo, "branch", "-q", "-D", "a")

    assert plan_stack.plan(["b"], verification="- ok") == 0
    lines = _rebases(capsys.readouterr().out)
    assert lines == [["rebase", "--onto", "main", tips["a2"], "b"]], lines

    _git(repo, *lines[0])  # the printed command, run as printed
    kept = _git(repo, "log", "--format=%s", "main..b").splitlines()
    assert kept == ["fix: b2", "fix: b1"], kept


def test_a_front_with_nothing_merged_yet_starts_at_the_fork_point(repo, capsys):
    """The first plan of a stack, whose front is a multi-commit branch off main: its base is
    the fork point, where `<tip>^` named its own first commit and would have dropped it."""
    tips = _two_commit_stack(repo)
    fork = _git(repo, "merge-base", "main", "a")
    assert plan_stack.plan(["a", "b"], verification="- ok") == 0
    lines = _rebases(capsys.readouterr().out)
    assert lines[0] == ["rebase", "--onto", "main", fork, "a"], lines
    assert tips["a1"] != fork


def test_the_title_is_the_changelog_heading_not_the_tip_subject(repo, capsys):
    _two_commit_stack(repo)
    assert plan_stack.plan(["a", "b"], verification="- ok") == 0
    out = capsys.readouterr().out
    assert "title: fix: the heading of a" in out, out
    assert "title: fix: a2" not in out
