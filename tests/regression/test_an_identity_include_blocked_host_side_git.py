"""Host-side git refused a repository whose own config pinned its identity by include.

Symptom: every write_file and search_files path inside such a repository is refused with
"its own config sets 'include.path', which is not on the list of keys host-side git
trusts", so the model cannot write or search there even though nothing in that repository
can run a program.

Cause: the path policy refuses any local or worktree config key off an allowlist, and the
two keys that pull in another file -- include.path and includeIf.<condition>.path -- were
never put on it.

Fix: allowlist them. That is safe because `git config --list` expands the included file
inline and reports each of its keys under the including file's scope, so every included
key is still judged by name: a planted core.fsmonitor behind an include is still refused,
and never runs. The first test pins that git behaviour; the others are the identity that
must be trusted and the planted key that must not be.
"""

import os
import shutil
import subprocess

import pytest

from claude_delegate_local import paths

needs_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="LAYER 4 UNPROVEN BY THIS RUN -- it needs git on PATH.",
)


def _git(repo, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *argv], check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def _repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    return path


def _include(repo, target, kind: str) -> None:
    """Point `repo`'s own config at `target`, unconditionally or behind a gitdir condition."""
    if kind == "include":
        _git(repo, "config", "--local", "include.path", str(target))
    else:
        # The condition names the work tree, which git matches against the git dir; a
        # trailing-slash `gitdir:<repo>/.git/` matches only things *inside* the git dir.
        # Real path, because git resolves symlinks in the git dir before matching.
        _git(
            repo,
            "config",
            "--local",
            f"includeIf.gitdir:{os.path.realpath(repo)}/.path",
            str(target),
        )


KINDS = ["include", "includeif"]


@needs_git
def test_git_lists_an_included_key_under_the_including_scope(tmp_path):
    """The fix rests on this: an included key is listed as `local` and judged by name."""
    repo = _repo(tmp_path / "repo")
    target = tmp_path / "identity.inc"
    target.write_text("[user]\n\tname = only-in-the-include\n", encoding="utf-8")
    _include(repo, target, "include")
    # Nothing else sets user.name, so a value can only come from the included file.
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--list", "--show-scope", "-z"],
        check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    fields = proc.stdout.split(b"\0")
    pairs = [
        (scope, entry.split(b"\n", 1))
        for scope, entry in zip(fields[0::2], fields[1::2], strict=False)
    ]
    included = [value for scope, (key, value) in pairs if key == b"user.name"]
    assert len(included) == 1, pairs
    assert (b"local", b"user.name", b"only-in-the-include") in [
        (scope, key, value) for scope, (key, value) in pairs
    ], pairs


@needs_git
@pytest.mark.parametrize("kind", KINDS)
def test_an_identity_include_is_trusted(tmp_path, kind):
    """A repository that only pulls in its identity must be read, not refused."""
    repo = _repo(tmp_path / "repo")
    target = tmp_path / "identity.inc"
    target.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n", encoding="utf-8"
    )
    _include(repo, target, kind)
    assert paths.untrusted_git_config(str(repo)) is None


@needs_git
@pytest.mark.parametrize("kind", KINDS)
def test_a_planted_key_behind_an_include_is_still_refused(tmp_path, kind):
    """The include is expanded, so a program-naming key in it is judged like any other."""
    repo = _repo(tmp_path / "repo")
    marker = tmp_path / "ran"
    target = tmp_path / "planted.inc"
    target.write_text(f"[core]\n\tfsmonitor = touch '{marker}'; false\n", encoding="utf-8")
    _include(repo, target, kind)
    assert paths.untrusted_git_config(str(repo)) == "core.fsmonitor"
    with pytest.raises(paths.PathPolicyError, match=r"core\.fsmonitor"):
        paths.gitignored([str(repo / "f.txt")])
    assert not marker.exists(), "gitignored ran the fsmonitor the include named"
