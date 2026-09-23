"""Host-side git ran commands a delegation's own repository config named (PLAN M13.8, R4).

`read_git` and the gitignore layer run the host's git, outside the sandbox. `run_bash` can
create a repository the point-in-time `.git/**` shadow never saw, and git honours
repository-local config, several keys of which name a program to run:

    core.fsmonitor          runs on `status` and `blame`
    diff.<driver>.textconv  runs on `show` and `blame`, named by .gitattributes
    filter.<driver>.clean   runs on `diff` and `blame`, named by .gitattributes
    any of these, in a submodule, through the top repository's `status`

Measured 2026-09-23 in throwaway repositories, git 2.43. A flag cannot close the driver
rows, because the driver's name is whatever the planted config chose, so the fix refuses a
repository whose own config carries a key off an allowlist, and keeps the flags as well.
The design is docs/specs/2026-09-23-host-acted-paths.md.

Every planted command writes a marker file, so "it ran" is a file on disk rather than a
guess from output. The controls are the same repositories without the planted key, and a
repository carrying the keys this project's own checkout carries, which must still work.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_delegate_local import paths, tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix" or shutil.which("git") is None,
    reason="LAYER 4 UNPROVEN BY THIS RUN -- run it under WSL, where the server lives.",
)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *argv], check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@example.com")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "one")
    (path / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(path, "commit", "-qam", "two")
    return path


def _plant(repo: Path, marker: Path, kind: str) -> None:
    """Configure one program-running key. Each writes `marker` when git runs it."""
    run = f"touch {marker}"
    if kind == "fsmonitor":
        _git(repo, "config", "core.fsmonitor", f"{run}; false")
    elif kind == "textconv":
        (repo / ".gitattributes").write_text("*.txt diff=planted\n", encoding="utf-8")
        _git(repo, "config", "diff.planted.textconv", f"{run}; cat")
    elif kind == "filter":
        (repo / ".git" / "info" / "attributes").write_text(
            "*.txt filter=planted\n", encoding="utf-8")
        _git(repo, "config", "filter.planted.clean", f"{run}; cat")
    # Dirty the work tree, so `diff` and `status` have content to compare.
    (repo / "f.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")


def cfg(root: Path) -> Config:
    return Config(workspace_roots=(str(root),))  # type: ignore[arg-type]


def _read_git(root: Path, repo: Path, command: str, args=(), files=()):
    request = {"command": command, "repo": str(repo), "args": list(args)}
    if files:
        request["paths"] = list(files)
    call = ToolUseBlock(id="call-1", name="read_git", input=request)
    return tools.execute_tool(cfg(root), call, tools.ALL_TOOL_NAMES)


# (planted key, command, args, paths): every row reproduced a planted run against the
# unfixed code when this was measured.
ROUTES = [
    ("fsmonitor", "status", (), ()),
    ("fsmonitor", "blame", (), ("f.txt",)),
    # `show`, not `log -p`: read_git refuses `-p`, and `show` prints the patch by default.
    ("textconv", "show", (), ()),
    ("textconv", "blame", (), ("f.txt",)),
    ("filter", "diff", (), ()),
    ("filter", "blame", (), ("f.txt",)),
]


@posix_only
@pytest.mark.parametrize("kind, command, args, files", ROUTES)
def test_read_git_does_not_run_a_planted_command(tmp_path, kind, command, args, files):
    repo = _repo(tmp_path / "planted")
    marker = tmp_path / "ran"
    _plant(repo, marker, kind)
    result = _read_git(tmp_path, repo, command, args, files)
    assert not marker.exists(), f"git {command} ran the planted {kind}"
    assert result.is_error, result.content
    assert "config" in result.content, result.content


@posix_only
@pytest.mark.parametrize("command, args, files", [(c, a, f) for _, c, a, f in ROUTES])
def test_read_git_still_works_without_the_planted_key(tmp_path, command, args, files):
    """The control: the same commands in the same shape of repository, nothing planted."""
    repo = _repo(tmp_path / "clean")
    (repo / "f.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = _read_git(tmp_path, repo, command, args, files)
    assert not result.is_error, result.content


@posix_only
def test_the_keys_a_real_checkout_carries_are_trusted(tmp_path):
    """What this repository's own config holds, less the values. A check that refused its
    own checkout would be switched off within a day."""
    repo = _repo(tmp_path / "real")
    for key, value in [
        ("credential.https://github.com.helper", "store"),
        ("lfs.repositoryformatversion", "0"),
        ("branch.main.vscode-merge-base", "origin/main"),
        ("remote.origin.url", "https://example.invalid/r.git"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ("remote.origin.gh-resolved", "base"),
    ]:
        _git(repo, "config", key, value)
    result = _read_git(tmp_path, repo, "log", ("-n", "1"))
    assert not result.is_error, result.content


@posix_only
def test_a_planted_submodule_is_not_reached_through_a_clean_top(tmp_path):
    """The top repository's config is clean and trusted; the submodule's is not, and
    `status` recursed into it. `diff.ignoreSubmodules=all` is what stops that."""
    inner = _repo(tmp_path / "inner")
    top = _repo(tmp_path / "top")
    _git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "m")
    _git(top, "commit", "-qm", "submodule")
    marker = tmp_path / "ran"
    _git(top / "m", "config", "core.fsmonitor", f"touch {marker}; false")
    (top / "m" / "f.txt").write_text("changed\n", encoding="utf-8")
    result = _read_git(tmp_path, top, "status")
    assert not marker.exists(), "status reached the submodule's fsmonitor"
    assert not result.is_error, result.content


@posix_only
def test_the_gitignore_layer_refuses_a_planted_repository(tmp_path):
    """Layer 4 runs `check-ignore` during an ordinary read. It must refuse, not answer."""
    repo = _repo(tmp_path / "planted")
    _plant(repo, tmp_path / "ran", "fsmonitor")
    with pytest.raises(paths.PathPolicyError, match=r"core\.fsmonitor"):
        paths.gitignored([str(repo / "f.txt")])


@posix_only
def test_repo_status_skips_a_planted_repository(tmp_path):
    """`repo_status` runs `status` for an abort report and swallows failures by design, so
    a planted repository is left out of the report rather than run."""
    repo = _repo(tmp_path / "planted")
    marker = tmp_path / "ran"
    _plant(repo, marker, "fsmonitor")
    assert paths.repo_status([str(repo)]) == {}
    assert not marker.exists(), "repo_status ran the planted fsmonitor"


def test_both_helpers_harden_every_call(monkeypatch):
    """Unit half: the argv and environment both helpers hand to git, pinned, so a later edit
    cannot drop one quietly. The environment half was defined and never called before."""
    seen: list[dict] = []

    def fake_run(argv, **kw):
        seen.append({"argv": list(argv), "env": kw.get("env")})
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/elsewhere/config")
    paths._git(["git", "status"])
    tools._run_git(["git", "--no-pager", "status"])
    # Literal rather than read back from the module, so the pin is a value and not a
    # reference to whatever the constant currently holds.
    hardening = ["-c", "core.fsmonitor=false", "-c", "diff.ignoreSubmodules=all"]
    assert len(seen) == 2
    for call in seen:
        assert call["env"] is not None, "git inherited the server's environment unfiltered"
        assert call["argv"][:5] == ["git", *hardening], call["argv"]
        assert "GIT_DIR" not in call["env"]
        assert "GIT_CONFIG_GLOBAL" not in call["env"]
        assert call["env"]["GIT_OPTIONAL_LOCKS"] == "0"
