"""The anti-drift check for the agent-format reference, negative-tested.

`docs/AGENTS.md` renders its format reference from the spec that ships inside the package.
Two copies of one fact is exactly the drift CLAUDE.md exists to stop, so the generator has
a `--check` mode and the gate runs it. A `--check` that cannot fail would put the second
copy back while looking like a guarantee, which is the class of bug this repository has
found six of -- so every test here feeds it a real divergence and asserts it is caught.

`--check` is driven as a subprocess rather than imported, because that is how the gate
drives it: importing would test a path nothing else uses.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "src/claude_delegate_local/skills/write-delegate-agent/SKILL.md"
RENDERED = REPO_ROOT / "docs/AGENTS.md"
GENERATOR = REPO_ROOT / "scripts/gen_agent_format_docs.py"


def run_check(cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/gen_agent_format_docs.py", "--check"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clone(tmp_path: pathlib.Path) -> pathlib.Path:
    """A throwaway tree holding just the three files the generator touches.

    Deliberately not named after anything the generator matches on: a fixture directory
    whose name collides with the pattern under test is how one check here was found to be
    passing against an unfixed bug.
    """
    root = tmp_path / "tree"
    (root / SPEC.parent.relative_to(REPO_ROOT)).mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SPEC, root / SPEC.relative_to(REPO_ROOT))
    shutil.copy2(RENDERED, root / RENDERED.relative_to(REPO_ROOT))
    shutil.copy2(GENERATOR, root / GENERATOR.relative_to(REPO_ROOT))
    return root


def test_the_committed_reference_is_current() -> None:
    """The repository itself is not stale. This is the check the gate runs."""
    result = run_check(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_edited_spec_is_reported_as_stale(clone: pathlib.Path) -> None:
    """The negative control, and the reason this file exists.

    Change one line of the spec without regenerating, and `--check` must notice *and name
    the changed line*. Asserting only that it failed would be satisfied by a generator
    that fails for an unrelated reason -- a missing marker, a bad path -- while drift went
    through untouched.
    """
    spec = clone / SPEC.relative_to(REPO_ROOT)
    text = spec.read_text(encoding="utf-8")
    changed = text.replace("first match wins", "LAST match wins", 1)
    assert changed != text, "the mutation did not apply -- this test proves nothing as written"
    spec.write_text(changed, encoding="utf-8")

    result = run_check(clone)

    assert result.returncode != 0, f"drift went unreported:\n{result.stdout}"
    assert "STALE" in result.stdout
    assert "LAST match wins" in result.stdout, (
        f"--check failed, but not because of the edit -- the diff never names it:\n{result.stdout}"
    )


def test_a_removed_marker_pair_is_refused_rather_than_guessed(clone: pathlib.Path) -> None:
    """Deleting the markers must not silently render nothing.

    A generator that quietly appended, or quietly skipped, would leave the reference
    unrendered while `--check` stayed green -- the check would exist and cover nothing.
    """
    rendered = clone / RENDERED.relative_to(REPO_ROOT)
    text = rendered.read_text(encoding="utf-8")
    stripped = text.replace("<!-- GEN:AGENT-FORMAT-FIELDS:START -->", "", 1)
    assert stripped != text
    rendered.write_text(stripped, encoding="utf-8")

    result = run_check(clone)

    assert result.returncode != 0
    assert "GEN:AGENT-FORMAT-FIELDS" in result.stdout + result.stderr
