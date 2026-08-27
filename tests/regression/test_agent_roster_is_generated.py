"""The agent roster in CONTRIBUTING.md is rendered from the agent files, not typed twice.

The table listing each subagent's model and effort duplicated `.claude/agents/*.md`
frontmatter. Every value happened to agree when this was written -- which is not the same
as being kept in agreement, and is exactly how the ancestor project ended up documenting
one setting as three different values.

These tests assert the drift check FIRES. A generator whose --check never fails is the
same trap as a gate check that cannot fail, one layer up.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts" / "gen_agents_docs.py"
AGENTS = ROOT / ".claude" / "agents"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GEN), *args],
                          cwd=ROOT, capture_output=True, text=True, check=False)


def test_the_committed_roster_matches_the_agent_files():
    out = run("--check")
    assert out.returncode == 0, f"roster is stale, run the generator:\n{out.stdout}"


@pytest.fixture
def restore_agents():
    """Snapshot the agent files, and put them back whatever the test does."""
    saved = {p: p.read_text(encoding="utf-8") for p in AGENTS.glob("*.md")}
    contributing = ROOT / "CONTRIBUTING.md"
    saved_doc = contributing.read_text(encoding="utf-8")
    yield
    for p, text in saved.items():
        p.write_text(text, encoding="utf-8", newline="")
    contributing.write_text(saved_doc, encoding="utf-8", newline="")


def test_changing_an_effort_makes_the_check_fail(restore_agents):
    """The whole point: a frontmatter edit that the table does not reflect must block."""
    target = next(AGENTS.glob("*.md"))
    text = target.read_text(encoding="utf-8")
    assert "effort:" in text, f"{target.name} has no effort key to perturb"
    perturbed = text.replace("effort: low", "effort: max", 1)
    if perturbed == text:
        perturbed = text.replace("effort: medium", "effort: max", 1)
    if perturbed == text:
        perturbed = text.replace("effort: high", "effort: max", 1)
    assert perturbed != text, "could not perturb the effort key"
    target.write_text(perturbed, encoding="utf-8", newline="")

    out = run("--check")
    assert out.returncode == 1, (
        f"the generator did not notice a changed effort -- the check cannot fail:\n"
        f"{out.stdout}")
    assert "STALE" in out.stdout


def test_a_new_agent_makes_the_check_fail(restore_agents):
    """Adding an agent without regenerating must block, not silently omit it."""
    extra = AGENTS / "zz-temp-probe.md"
    extra.write_text(
        "---\nname: zz-temp-probe\ndescription: Temporary.\nmodel: haiku\neffort: low\n---\n",
        encoding="utf-8", newline="")
    try:
        out = run("--check")
        assert out.returncode == 1, f"a new agent went unnoticed:\n{out.stdout}"
    finally:
        extra.unlink()


def test_a_missing_effort_key_is_rendered_visibly(restore_agents):
    """A misspelled key bills the default tier in silence; the table must not hide it."""
    target = next(AGENTS.glob("*.md"))
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("effort:", "reasoning_effort:", 1),
                      encoding="utf-8", newline="")

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import importlib

        import gen_agents_docs
        importlib.reload(gen_agents_docs)
        block = gen_agents_docs.render()
    finally:
        sys.path.pop(0)

    assert gen_agents_docs.MISSING in block, (
        "a missing effort key rendered as something plausible instead of visibly absent")
