"""The agent roster in CONTRIBUTING.md is rendered from the agent files, not typed twice.

The table listing each subagent's model and effort duplicated `.claude/agents/*.md`
frontmatter. Every value happened to agree when this was written -- which is not the same
as being kept in agreement, and is exactly how the ancestor project ended up documenting
one setting as three different values.

These tests assert the drift check FIRES. A generator whose --check never fails is the
same trap as a gate check that cannot fail, one layer up.

Every test that perturbs anything does so in a throwaway tree. The first version of this
file edited the real `.claude/agents/*.md` and `CONTRIBUTING.md` and put them back in a
fixture teardown. That teardown runs on assertion failure but not on a killed process, so
an interrupted run left the working tree corrupt -- and it made the suite unsafe to run in
parallel, where another worker reading those files mid-test saw the perturbed copy and
failed. Found by running the suite under `pytest -n auto`, which failed intermittently on
the read-only test below.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts" / "gen_agents_docs.py"
AGENTS = ROOT / ".claude" / "agents"


@pytest.fixture
def roster(tmp_path: Path) -> Path:
    """A throwaway copy of the roster inputs, and the generator pointed at it.

    The generator resolves ROOT from its own location, so copying the script into a temp
    tree makes that tree the repository under test. The same trick the gate's `repo`
    fixtures use in the sibling regression tests, for the same reason: nothing here touches
    the real tree, so there is nothing to restore and nothing to corrupt.
    """
    tree = tmp_path / "r"
    (tree / "scripts").mkdir(parents=True)
    (tree / ".claude" / "agents").mkdir(parents=True)
    shutil.copy2(GEN, tree / "scripts" / "gen_agents_docs.py")
    shutil.copy2(ROOT / "CONTRIBUTING.md", tree / "CONTRIBUTING.md")
    for agent in AGENTS.glob("*.md"):
        shutil.copy2(agent, tree / ".claude" / "agents" / agent.name)
    return tree


def run(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "gen_agents_docs.py"), *args],
        cwd=tree, capture_output=True, text=True, check=False)


def perturb_an_effort(tree: Path) -> Path:
    """Change one agent's effort tier without regenerating the table."""
    target = next((tree / ".claude" / "agents").glob("*.md"))
    text = target.read_text(encoding="utf-8")
    assert "effort:" in text, f"{target.name} has no effort key to perturb"
    for tier in ("effort: low", "effort: medium", "effort: high"):
        if tier in text:
            target.write_text(text.replace(tier, "effort: max", 1),
                              encoding="utf-8", newline="")
            return target
    raise AssertionError("could not perturb the effort key")


def test_the_committed_roster_matches_the_agent_files():
    """The one test that must look at the real tree, because that is the claim.

    Read-only: a `--check` run of the committed generator against the committed files. It
    asserts nothing about a perturbation, so it needs no copy -- and it is precisely the
    test the old mutating fixture used to break when the two ran concurrently.
    """
    out = subprocess.run([sys.executable, str(GEN), "--check"],
                         cwd=ROOT, capture_output=True, text=True, check=False)
    assert out.returncode == 0, f"roster is stale, run the generator:\n{out.stdout}"


def test_changing_an_effort_makes_the_check_fail(roster):
    """The whole point: a frontmatter edit that the table does not reflect must block."""
    perturb_an_effort(roster)

    out = run(roster, "--check")

    assert out.returncode == 1, (
        f"the generator did not notice a changed effort -- the check cannot fail:\n"
        f"{out.stdout}")
    assert "STALE" in out.stdout


def test_a_new_agent_makes_the_check_fail(roster):
    """Adding an agent without regenerating must block, not silently omit it."""
    (roster / ".claude" / "agents" / "zz-temp-probe.md").write_text(
        "---\nname: zz-temp-probe\ndescription: Temporary.\nmodel: haiku\neffort: low\n---\n",
        encoding="utf-8", newline="")

    out = run(roster, "--check")

    assert out.returncode == 1, f"a new agent went unnoticed:\n{out.stdout}"


def test_regenerating_makes_the_check_pass_again(roster):
    """The other direction. Without it, a --check that always failed would pass the two
    tests above while being useless."""
    perturb_an_effort(roster)
    assert run(roster, "--check").returncode == 1

    assert run(roster).returncode == 0

    assert run(roster, "--check").returncode == 0


def test_a_missing_effort_key_is_rendered_visibly(roster):
    """A misspelled key bills the default tier in silence; the table must not hide it."""
    target = next((roster / ".claude" / "agents").glob("*.md"))
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("effort:", "reasoning_effort:", 1),
                      encoding="utf-8", newline="")

    # Import the *copied* generator, so its ROOT is the throwaway tree. Loading it by path
    # rather than by name also avoids the reload-a-shared-module trick the previous version
    # used, which mutated interpreter state that other tests could observe.
    spec = importlib.util.spec_from_file_location(
        "gen_agents_docs_probe", roster / "scripts" / "gen_agents_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.MISSING in module.render(), (
        "a missing effort key rendered as something plausible instead of visibly absent")
