"""The gitignore layer read a git failure as "not ignored", so it failed open.

`_repo_top` treated any non-zero `rev-parse` as "outside every repository", and
`gitignored` skipped any `check-ignore` exit other than 0 or 1. Both are right for the one
failure that means "not a repository" and wrong for every other: an ownership refusal or
a damaged repository reported every file as not ignored, and layer 4 passed having looked
at nothing -- the shape the module's own header says a layer must never take.

Git's message is read under `LC_ALL=C`, because the one exit that is not an error is
recognised by its words and a translated git would otherwise say them differently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_delegate_local import paths

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def damaged_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (r / "build.log").write_text("x\n", encoding="utf-8")
    _git(r, "add", ".gitignore")
    # An index git cannot read: `check-ignore` exits 128 inside a repository it found.
    (r / ".git" / "index").write_bytes(b"not an index")
    return r


@pytest.mark.skipif(os.name != "posix", reason="realpath under tmp_path needs POSIX")
def test_a_damaged_repository_refuses_rather_than_reporting_nothing_ignored(damaged_repo):
    target = str(damaged_repo / "build.log")
    with pytest.raises(paths.PathPolicyError):
        paths.gitignored([target])


def test_a_refusal_other_than_not_a_repository_is_an_error(monkeypatch):
    def fake(args, stdin=None):
        return subprocess.CompletedProcess(
            args, 128, b"", b"fatal: detected dubious ownership in repository at '/x'\n")

    monkeypatch.setattr(paths, "_git", fake)
    with pytest.raises(paths.PathPolicyError):
        paths._repo_top("/x")


def test_outside_a_repository_is_still_not_an_error(monkeypatch):
    for message in (
        b"fatal: not a git repository (or any of the parent directories): .git\n",
        b"fatal: not a git repository (or any parent up to mount point /mnt)\n",
    ):
        monkeypatch.setattr(paths, "_git", lambda args, stdin=None, m=message:
                            subprocess.CompletedProcess(args, 128, b"", m))
        assert paths._repo_top("/x") is None


def test_git_runs_in_the_c_locale(monkeypatch):
    seen: list[dict] = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    paths._git(["git", "--version"])
    assert seen[0]["env"]["LC_ALL"] == "C"
