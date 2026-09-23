"""`read_git` refused a `--` in `args`, the separator git itself uses.

Across every transcript, 7 of 132 `read_git` calls put `--` and file paths in `args`, as
anyone who knows git would. Each was refused with "Do not pass '--' yourself; put file
paths in 'paths'", and 6 of the 7 re-issued the same call with `paths` on the next turn --
a turn spent on a spelling. The meaning was never in doubt: what follows `--` is paths. So
it is taken as `paths` now, and checked exactly as `paths` are: relative, inside the
repository, and through the path policy for the commands that return file contents.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix" or shutil.which("git") is None,
    reason="LAYER 1 UNPROVEN BY THIS RUN -- run it under WSL, where the server lives.",
)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *argv], check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "user.email", "t@example.com")
    (r / "a.txt").write_text("alpha\n", encoding="utf-8")
    (r / "b.txt").write_text("beta\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "both")
    (r / "a.txt").write_text("alpha\nmore\n", encoding="utf-8")
    _git(r, "commit", "-qam", "only-a-changed")
    return r


def _read_git(root: Path, command: str, args: list[str], paths: list[str] | None = None):
    request: dict[str, object] = {"command": command, "repo": str(root), "args": args}
    if paths is not None:
        request["paths"] = paths
    call = ToolUseBlock(id="call-1", name="read_git", input=request)
    cfg = Config(workspace_roots=(str(root.parent),))  # type: ignore[arg-type]
    return tools.execute_tool(cfg, call, tools.ALL_TOOL_NAMES)


@posix_only
def test_paths_after_a_separator_limit_the_command(repo):
    result = _read_git(repo, "log", ["--oneline", "--", "b.txt"])
    assert not result.is_error, result.content
    assert "both" in result.content
    assert "only-a-changed" not in result.content, "the path after -- was not applied"


@posix_only
def test_they_join_any_paths_given_the_ordinary_way(repo):
    result = _read_git(repo, "log", ["--oneline", "--", "b.txt"], paths=["a.txt"])
    assert not result.is_error, result.content
    assert "both" in result.content and "only-a-changed" in result.content


@posix_only
def test_a_path_after_the_separator_is_still_checked(repo):
    """The control: moving a token into `paths` must not move it past the checks on
    `paths`. One that climbs out of the repository is refused either way."""
    result = _read_git(repo, "log", ["--oneline", "--", "../elsewhere"])
    assert result.is_error, result.content
    assert "climbs out" in result.content, result.content


@posix_only
def test_without_a_separator_nothing_changes(repo):
    result = _read_git(repo, "log", ["--oneline", "-n", "1"])
    assert not result.is_error, result.content
    assert "only-a-changed" in result.content and "both" not in result.content
