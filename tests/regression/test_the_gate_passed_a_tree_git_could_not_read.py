"""The gate passed a tree git could not read, having checked nothing.

`run()` returned an empty string for a failed command, and most checks read empty as clean:
no files tracked means no secret paths, no documents means no dangling references. So a
gate whose git calls all failed reported PASS, which is the fail-open shape this repository
treats as its worst kind of check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "docs_gate.py"


def _gate_outside_any_repository(tmp_path: Path) -> subprocess.CompletedProcess:
    tree = tmp_path / "not-a-repo"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(GATE, tree / "scripts" / "docs_gate.py")
    shutil.copy(REPO / "scripts" / "docs_ownership.toml", tree / "scripts")
    shutil.copytree(REPO / "security", tree / "security")
    # An identity the copied allowlist accepts, so the only thing wrong with this tree is
    # that git cannot read it. Without this the identity check blocks on the machine's own
    # address, and a test asserting "the gate failed" passes against the unfixed gate.
    (tree / "security" / "allowed_emails.txt").write_text("t@example.com\n", encoding="utf-8")
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\temail = t@example.com\n\tname = t\n", encoding="utf-8")
    # GIT_CEILING_DIRECTORIES stops git walking up into whatever repository holds tmp_path.
    env = {
        **os.environ,
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        [sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
        cwd=tree, capture_output=True, text=True, env=env,
    )


def test_the_gate_fails_when_git_cannot_read_the_tree(tmp_path: Path) -> None:
    proc = _gate_outside_any_repository(tmp_path)
    assert proc.returncode != 0, proc.stdout
    # The failure has to be the git call, named -- not some other check that happened to block.
    assert "could not run: `git " in proc.stdout, proc.stdout


def test_a_passed_range_is_required_and_a_missing_default_is_a_skip(tmp_path: Path) -> None:
    """CI always passes `--diff`, so a bad one must block; a clone with no `origin/main` has
    no default to read, and saying so is a SKIP rather than the clean answer it used to be."""
    proc = _gate_outside_any_repository(tmp_path)   # builds the tree; git init it now
    tree = tmp_path / "not-a-repo"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
           "GIT_CONFIG_NOSYSTEM": "1"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "chore: base"]):
        subprocess.run(cmd, cwd=tree, env=env, capture_output=True, check=True)

    def gate(*extra: str) -> str:
        return subprocess.run(
            [sys.executable, "scripts/docs_gate.py", "--mode", "ci", *extra],
            cwd=tree, env=env, capture_output=True, text=True,
        ).stdout

    passed = gate("--diff", "no-such-ref...HEAD")
    assert "BLOCK [identity] could not run" in passed, passed
    default = gate()
    assert "SKIP  [identity] no origin/main" in default, default
    assert proc.returncode != 0


def test_a_required_command_that_fails_raises(tmp_path: Path, monkeypatch) -> None:
    sys.path.insert(0, str(GATE.parent))
    try:
        import docs_gate
    finally:
        sys.path.remove(str(GATE.parent))
    monkeypatch.setattr(docs_gate, "ROOT", tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert docs_gate.run("git", "ls-files") == ""   # the lenient default is unchanged
    try:
        docs_gate.run("git", "ls-files", required=True)
    except docs_gate.GitFailed as e:
        assert "ls-files" in str(e)
    else:
        raise AssertionError("a failed required git command returned instead of raising")
