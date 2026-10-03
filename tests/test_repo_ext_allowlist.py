"""A repository may ADD extensions to the extension allowlist (layer 2).

`DELEGATE_EXT_ALLOWLIST` is the operator's list, shared across every project an operator
may delegate. A repository that needs a file type only it carries would otherwise have to
ask the operator to widen a list that governs other projects too -- which is the bug a
per-repository addition exists to avoid. A repo adds to the list without replacing it: a
`<root>/.claude/delegate-local.toml` naming extra extensions for files under that root only,
and only as deep as the deepest workspace root containing the file.

These tests are written first and fail until the feature exists. Only the protected-path
guard at the bottom is expected green already, because `security/protected_globs.txt` has
covered `.claude/**` since before this feature.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_delegate_local import config, paths, server, tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the path policy resolves POSIX paths; the server is POSIX-only",
)

ROOTS = {"DELEGATE_WORKSPACE_ROOTS": "/tmp/proj"}

_REPO = Path(__file__).resolve().parent.parent
SHIPPED_SECRET_GLOBS = str(_REPO / "security" / "secret_globs.txt")
SHIPPED_PROTECTED_GLOBS = str(_REPO / "security" / "protected_globs.txt")


def cfg_for(root: Path, **over) -> Config:
    """A config rooted at `root`, with the shipped deny/protect lists and gitignore off.

    Layer 4 shells out to git, so it is off unless a test is about it; the two lists are
    the shipped ones because these tests are about the real guard, not a fixture copy.
    """
    kw = {
        "workspace_roots": (os.path.realpath(root),),
        "respect_gitignore": False,
        "secret_globs_file": SHIPPED_SECRET_GLOBS,
        "protected_globs_file": SHIPPED_PROTECTED_GLOBS,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def write_repo_settings(root: Path, content: str) -> Path:
    target = Path(root) / paths.REPO_SETTINGS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# --- a fake MCP context whose client lists roots, as the ADR-0110 tests drive -----------


class _Root:
    def __init__(self, path: Path) -> None:
        self.uri = Path(path).as_uri()


class _Params:
    def __init__(self, roots: list[Path] | None) -> None:
        self.capabilities = SimpleNamespace(
            roots=SimpleNamespace() if roots is not None else None
        )


class _Session:
    def __init__(self, roots: list[Path] | None) -> None:
        self.client_params = _Params(roots)


class _Ctx:
    """Enough of a fastmcp `Context` for `session_scope`: a session and a roots answer."""

    def __init__(self, roots: list[Path] | None) -> None:
        self.session = _Session(roots)
        self._roots = list(roots or [])

    async def list_roots(self):
        return [_Root(r) for r in self._roots]


# --- 1. loading ------------------------------------------------------------------------


@posix_only
def test_repo_extras_are_normalised_and_absent_roots_are_omitted(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    write_repo_settings(a, 'ext_allowlist_extra = [".JSONL", "foo"]\n')
    loaded = paths.load_repo_ext_allowlists([os.path.realpath(a), os.path.realpath(b)])
    assert loaded == ((os.path.realpath(a), (".jsonl", ".foo")),)


@posix_only
@pytest.mark.parametrize(
    "content",
    [
        "this is not toml",  # does not parse
        "unknown_key = ['.foo']",  # a key that is not ext_allowlist_extra
        "ext_allowlist_extra = 5",  # not a list
        "ext_allowlist_extra = [1, 2]",  # a list that is not of strings
        "ext_allowlist_extra = ['a/b']",  # an entry containing '/'
        "ext_allowlist_extra = ['a\\b']",  # an entry containing '\\'
        "ext_allowlist_extra = ['a*b']",  # an entry containing '*'
    ],
)
def test_a_malformed_repo_settings_file_raises_naming_the_file(tmp_path, content):
    a = tmp_path / "a"
    a.mkdir()
    repo_file = write_repo_settings(a, content)
    with pytest.raises(paths.RepoSettingsError) as e:
        paths.load_repo_ext_allowlists([os.path.realpath(a)])
    assert str(repo_file) in str(e.value)


# --- 2. layer 2 with repo extras --------------------------------------------------------


@posix_only
def test_repo_extras_apply_only_under_the_root_that_carries_them(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    conf = cfg_for(
        tmp_path,
        workspace_roots=(os.path.realpath(a), os.path.realpath(b)),
        repo_ext_allowlists=((os.path.realpath(a), (".foo",)),),
    )
    assert paths.extension_refusal(conf, str(a / "x.foo")) is None
    # The negative control: a sibling root with no extras gets nothing.
    assert paths.extension_refusal(conf, str(b / "x.foo")) is not None


@posix_only
def test_the_deepest_workspace_root_wins_for_repo_extras(tmp_path):
    a = tmp_path / "a"
    sub = a / "sub"
    a.mkdir()
    sub.mkdir()
    conf = cfg_for(
        tmp_path,
        workspace_roots=(os.path.realpath(a), os.path.realpath(sub)),
        repo_ext_allowlists=((os.path.realpath(a), (".foo",)),),
    )
    assert paths.extension_refusal(conf, str(a / "x.foo")) is None
    # A/sub is the deepest root containing this file and it carries no extras, so the
    # file does not get A's list: the deepest root decides, not the nearest one with one.
    assert paths.extension_refusal(conf, str(sub / "x.foo")) is not None


# --- 3. extras cannot override layer 3 ---------------------------------------------------


@posix_only
def test_repo_extras_cannot_override_the_secret_denylist(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    key = a / "key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    conf = cfg_for(a, repo_ext_allowlists=((os.path.realpath(a), (".pem",)),))
    survivors, refusals = paths.resolve_files(conf, [str(key)])
    assert survivors == ()
    (refusal,) = refusals
    assert refusal.layer == paths.LAYER_SECRET


# --- 4. derived, not a setting -----------------------------------------------------------


def test_repo_ext_allowlists_is_derived_and_never_a_setting():
    loaded = config.load(
        {**ROOTS, "DELEGATE_REPO_EXT_ALLOWLISTS": os.pathsep.join([".foo", ".bar"])}
    )
    assert loaded.repo_ext_allowlists == ()
    described = {row["field"] for row in config.describe()}
    assert "repo_ext_allowlists" not in described


# --- 5. end to end through session_scope ---------------------------------------------------


@posix_only
def test_session_scope_carries_the_sessions_repo_extras(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    write_repo_settings(a, 'ext_allowlist_extra = [".foo"]\n')
    conf = cfg_for(tmp_path)
    scope = asyncio.run(server.session_scope(conf, _Ctx([a])))
    assert (os.path.realpath(a), (".foo",)) in scope.cfg.repo_ext_allowlists


@posix_only
def test_a_malformed_repo_settings_file_fails_the_session(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    repo_file = write_repo_settings(a, "this is not toml")
    conf = cfg_for(tmp_path)
    with pytest.raises(Exception) as e:
        asyncio.run(server.session_scope(conf, _Ctx([a])))
    assert str(repo_file) in str(e.value)


@posix_only
def test_the_run_command_reads_the_repo_extras_too(tmp_path, monkeypatch):
    """`run` has no MCP session, so `session_scope` never fills the extras for it."""
    from claude_delegate_local import run_task

    a = tmp_path / "a"
    a.mkdir()
    write_repo_settings(a, 'ext_allowlist_extra = [".foo"]\n')
    monkeypatch.setattr(config, "load", lambda: cfg_for(a))
    assert run_task._load_config().repo_ext_allowlists == ((os.path.realpath(a), (".foo",)),)


# --- 6. the guard that should already hold -------------------------------------------------


@posix_only
def test_write_file_to_a_repo_settings_file_is_refused(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / ".claude").mkdir()
    conf = cfg_for(a)
    call = ToolUseBlock(
        id="call-1",
        name="write_file",
        input={
            "path": str(a / ".claude" / "delegate-local.toml"),
            "content": "x\n",
        },
    )
    result = tools.execute_tool(conf, call, tools.ALL_TOOL_NAMES)
    assert result.is_error, result.content
    assert "protected" in result.content


# --- 7. repo extras reach resolve_files, search_files and read_git ----------------------


def _git(repo: Path, *argv: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(["git", *argv], cwd=repo, env=env, check=True,
                          capture_output=True, text=True)
    return done.stdout


@posix_only
def test_resolve_files_keeps_a_repo_extra_file_and_refuses_a_sibling(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.foo").write_text("hello\n", encoding="utf-8")
    (b / "x.foo").write_text("hello\n", encoding="utf-8")
    conf = cfg_for(
        tmp_path,
        workspace_roots=(os.path.realpath(a), os.path.realpath(b)),
        repo_ext_allowlists=((os.path.realpath(a), (".foo",)),),
    )
    survivors, refusals = paths.resolve_files(conf, [str(a / "x.foo"), str(b / "x.foo")])
    assert [s.posix for s in survivors] == [os.path.realpath(str(a / "x.foo"))]
    (refusal,) = refusals
    assert refusal.layer == paths.LAYER_EXT


@posix_only
def test_search_files_finds_a_repo_extra_file_but_not_in_a_root_without_extras(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.foo").write_text("the needle is here\n", encoding="utf-8")
    (b / "x.foo").write_text("the needle is here\n", encoding="utf-8")
    conf = cfg_for(
        tmp_path,
        workspace_roots=(os.path.realpath(a), os.path.realpath(b)),
        repo_ext_allowlists=((os.path.realpath(a), (".foo",)),),
    )

    def search(root: Path):
        call = ToolUseBlock(
            id="call-1", name="search_files",
            input={"path": str(root), "pattern": "needle"},
        )
        return tools.execute_tool(conf, call, tools.ALL_TOOL_NAMES)

    found = search(a)
    assert not found.is_error, found.content
    assert "the needle is here" in found.content
    absent = search(b)
    assert "No line matched" in absent.content


@posix_only
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")
def test_read_git_show_obeys_a_repo_extras_for_the_commit(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    _git(a, "init", "-q", "-b", "main")
    (a / "x.foo").write_text("FOOMARKER\n", encoding="utf-8")
    _git(a, "add", "x.foo")
    _git(a, "commit", "-q", "-m", "feat: foo")

    def show(conf: Config):
        call = ToolUseBlock(
            id="call-1", name="read_git",
            input={"repo": str(a), "command": "show", "args": ["HEAD"]},
        )
        return tools.execute_tool(conf, call, tools.ALL_TOOL_NAMES)

    allowed = cfg_for(a, repo_ext_allowlists=((os.path.realpath(a), (".foo",)),))
    shown = show(allowed)
    assert not shown.is_error, shown.content
    assert "FOOMARKER" in shown.content

    withheld = show(cfg_for(a))
    assert "FOOMARKER" not in withheld.content
