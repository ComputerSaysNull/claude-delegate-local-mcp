"""A gitignored file the caller or the model named by path was refused (ADR-0122).

Layer 4 refused every path git ignores, for reads as well as writes. Seen 2026-10-12: two
delegations needed a hand-off document kept out of git on purpose, containing no secret,
and could only ask the person to paste it. The secret checks are layer 3's -- names, and
since ADR-0121 key armour and API tokens by their bytes -- and layer 4 was only ever a net,
one the sandbox does not cast: a shell with a workdir could `cat` the same file.

So a named read no longer asks layer 4. A write still does: a change to an ignored file never
shows in `git diff`, and an ignored folder such as `.venv` holds code the host runs outside
the sandbox. A search, and a glob in `files[]`, still leave ignored files out, so build
output and vendored bulk stay out of what the model finds -- it reads an ignored file when
it already knows the path.
"""

import os
import subprocess

import pytest

from claude_delegate_local.config import Config
from claude_delegate_local.paths import (
    LAYER_GITIGNORE,
    LAYER_SECRET,
    PathRefused,
    expand_globs,
    resolve_all,
    resolve_files,
    resolve_permitted,
)


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "nogitconfig"),
           "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    (root / ".gitignore").write_text("handoff/\n", encoding="utf-8")
    (root / "handoff").mkdir()
    (root / "handoff" / "spec.md").write_text("# Spec\n", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\n", encoding="utf-8")
    return root


def _cfg(root) -> Config:
    return Config(  # type: ignore[arg-type]
        workspace_roots=(os.path.realpath(root),), respect_gitignore=True)


# ---- what changed: a named read ------------------------------------------------------


def test_a_named_ignored_file_can_be_read(tmp_path):
    root = _repo(tmp_path)
    spec = str(root / "handoff" / "spec.md")

    assert len(resolve_all(_cfg(root), [spec])) == 1
    survivors, refusals = resolve_files(_cfg(root), [spec])
    assert len(survivors) == 1 and not refusals, refusals


def test_a_glob_leaves_ignored_matches_out(tmp_path):
    """A glob is a search in shorthand: `**/*.md` must not pull in what git ignores."""
    root = _repo(tmp_path)

    named, refusals = expand_globs(_cfg(root), [f"{root}/**/*.md"])

    assert not refusals, refusals
    assert [os.path.basename(p) for p in named] == ["notes.md"], named


def test_a_glob_matching_only_ignored_files_says_so(tmp_path):
    """Not "matched no files", which would send the caller hunting for a typo."""
    root = _repo(tmp_path)

    named, refusals = expand_globs(_cfg(root), [f"{root}/handoff/*.md"])

    assert named == ()
    (refusal,) = refusals
    assert refusal.layer == LAYER_GITIGNORE, refusal
    assert "name" in refusal.remedy, refusal.remedy


# ---- what did not change ---------------------------------------------------------------


def test_writing_an_ignored_file_is_still_refused(tmp_path):
    root = _repo(tmp_path)

    for path, must_exist in ((root / "handoff" / "spec.md", True),
                             (root / "handoff" / "new.md", False)):
        with pytest.raises(PathRefused) as caught:
            resolve_all(_cfg(root), [str(path)], writing=True, must_exist=must_exist)
        assert caught.value.refusals[0].layer == LAYER_GITIGNORE


def test_a_search_still_leaves_ignored_files_out(tmp_path):
    root = _repo(tmp_path)
    candidates = [str(root / "handoff" / "spec.md"), str(root / "notes.md")]

    kept = resolve_permitted(_cfg(root), candidates)

    assert [os.path.basename(k.posix) for k in kept] == ["notes.md"]


def test_a_secret_in_an_ignored_folder_is_still_refused_by_name(tmp_path):
    root = _repo(tmp_path)
    secret = root / "handoff" / "deploy_secret.md"
    secret.write_text("nothing\n", encoding="utf-8")

    with pytest.raises(PathRefused) as caught:
        resolve_all(_cfg(root), [str(secret)])

    assert caught.value.refusals[0].layer == LAYER_SECRET
