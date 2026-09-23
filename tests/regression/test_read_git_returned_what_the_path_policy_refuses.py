"""`read_git` handed back file contents the path policy refuses.

Observed in use: a delegation refused `security/secret_globs.txt` by `read_file` read it
through `read_git` instead. Every non-flag argument was passed to git as a revision, so
`show <rev>:<path>`, a blob id from `ls-files --stage`, a whole commit's patch and `blame`
all returned the bytes of files layers 2 and 3 exist to withhold -- including ones since
deleted, which only history still holds.

The fix answers each route the way `read_file` would: a revision must name a commit, a
path must pass the extension allowlist and the secret denylist, a patch section for a
refused path is withheld, and output carrying key material is refused whole. The cases
that must keep working sit beside the ones that must not, so a fix that refused
everything would fail here too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH"),
    pytest.mark.skipif(os.name != "posix", reason="layer 1 needs a POSIX filesystem"),
]

SECRET = "TOPSECRET-VALUE-7731"
# Assembled from fragments so this file is not itself key material to the sandbox's scan.
PEM = "-----BEGIN " + "PRIVATE KEY-----"


def _git(repo: Path, *argv: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(["git", *argv], cwd=repo, env=env, check=True,
                          capture_output=True, text=True)
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "kept.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "kept.py")
    _git(r, "commit", "-q", "-m", "feat: first")
    # One commit touching an allowed file, a denylisted one and an unlisted extension.
    (r / "kept.py").write_text("x = 2\n", encoding="utf-8")
    (r / "deploy_secret.py").write_text(f"TOKEN = '{SECRET}'\n", encoding="utf-8")
    (r / "blob.bin").write_text(f"{SECRET}\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat: second")
    # Key material in a file whose name and extension are both allowed.
    (r / "notes.py").write_text(f"# {PEM}\n", encoding="utf-8")
    _git(r, "add", "notes.py")
    _git(r, "commit", "-q", "-m", "feat: third")
    return r


def read_git(repo: Path, **args):
    cfg = Config(workspace_roots=(str(repo.parent),), respect_gitignore=False)
    call = ToolUseBlock(id="call-1", name="read_git", input={"repo": str(repo), **args})
    return tools.execute_tool(cfg, call, tools.ALL_TOOL_NAMES)


def refused(result) -> bool:
    return result.is_error and SECRET not in result.content


# --- the routes that returned refused bytes ---------------------------------------------


def test_a_rev_colon_path_is_refused(repo):
    result = read_git(repo, command="show", args=["HEAD~1:deploy_secret.py"])
    assert refused(result), result.content


def test_the_denylist_itself_is_refused_by_rev_colon_path(repo):
    """The observed case: a file `read_file` refused, read through history instead."""
    (repo / "security").mkdir()
    (repo / "security" / "secret_globs.txt").write_text("*secret*\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: list")
    result = read_git(repo, command="show", args=["HEAD:security/secret_globs.txt"])
    assert result.is_error and "*secret*" not in result.content, result.content


def test_a_blob_id_is_refused(repo):
    stage = _git(repo, "ls-files", "--stage", "deploy_secret.py")
    blob = stage.split()[1]
    result = read_git(repo, command="show", args=[blob])
    assert refused(result), result.content


def test_a_commit_patch_withholds_the_refused_files(repo):
    result = read_git(repo, command="show", args=["HEAD~1"])
    assert not result.is_error, result.content
    assert SECRET not in result.content
    assert "x = 2" in result.content, "the allowed file's change must still be shown"
    assert "deploy_secret.py" in result.content and "blob.bin" in result.content


def test_a_diff_withholds_the_refused_files(repo):
    result = read_git(repo, command="diff", args=["HEAD~2", "HEAD~1"])
    assert not result.is_error, result.content
    assert SECRET not in result.content
    assert "x = 2" in result.content


def test_a_diff_range_withholds_the_refused_files(repo):
    result = read_git(repo, command="diff", args=["HEAD~2..HEAD~1"])
    assert not result.is_error, result.content
    assert SECRET not in result.content


def test_blame_of_a_denylisted_path_is_refused(repo):
    result = read_git(repo, command="blame", args=["HEAD"], paths=["deploy_secret.py"])
    assert refused(result), result.content


def test_blame_of_an_unlisted_extension_is_refused(repo):
    result = read_git(repo, command="blame", args=["HEAD"], paths=["blob.bin"])
    assert refused(result), result.content


def test_output_carrying_key_material_is_refused(repo):
    result = read_git(repo, command="show", args=["HEAD"])
    assert result.is_error and "key material" in result.content, result.content
    assert "# " + PEM not in result.content, "the file line itself must not come back"


# --- what must keep working -------------------------------------------------------------


def test_blame_of_an_allowed_file_still_works(repo):
    result = read_git(repo, command="blame", args=["-L", "1,1"], paths=["kept.py"])
    assert not result.is_error, result.content
    assert "x = 2" in result.content


def test_a_diff_with_a_context_flag_still_works(repo):
    result = read_git(repo, command="diff", args=["--unified=1", "HEAD~2", "HEAD~1"],
                      paths=["kept.py"])
    assert not result.is_error, result.content
    assert "x = 2" in result.content


def test_show_stat_of_a_commit_still_lists_every_file(repo):
    result = read_git(repo, command="show", args=["--stat", "HEAD~1"])
    assert not result.is_error, result.content
    assert "deploy_secret.py" in result.content
