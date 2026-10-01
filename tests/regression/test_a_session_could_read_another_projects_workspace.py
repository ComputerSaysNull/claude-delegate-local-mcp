"""A session could read another project's workspace.

Every session's server read one `DELEGATE_WORKSPACE_ROOTS`, so a session opened in project B
could read project A whenever both were listed -- and listing both is what an operator
hopping between projects ends up doing, because editing the list on every switch is the
alternative. The client already says which folders a session works in (MCP roots: Claude
Code lists its working directory and every `--add-dir`), so the server narrows the configured
roots to those, and the configured list becomes a ceiling rather than a grant.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastmcp import Client

from claude_delegate_local import server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="the path policy resolves POSIX paths; the server is POSIX-only")


def projects(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        p.mkdir()
        (p / "x.py").write_text("x = 1\n", encoding="utf-8")
    return a, b


def call(config, tool: str, args: dict, *, roots=None, sent=None) -> dict:
    """One tool call over a real MCP session whose client lists `roots`, or none at all."""
    sent = [] if sent is None else sent
    mcp = server.build(config, registry(entry()), DoubleCache(config, recording_handler(sent)))
    if tool.startswith("delegate"):
        args.setdefault("effort", "inherit")

    async def go():
        kw = {} if roots is None else {"roots": [Path(r).as_uri() for r in roots]}
        async with Client(mcp, **kw) as client:
            return await answered(client, tool, args)

    return asyncio.run(go())


@posix_only
def test_a_file_in_another_project_is_refused_when_the_client_lists_only_this_one(tmp_path):
    a, b = projects(tmp_path)
    sent: list = []
    with pytest.raises(Exception) as e:  # fastmcp re-raises its own error type
        call(files_cfg(tmp_path), "delegate_readonly",
             {"task": "read it", "files": [str(b / "x.py")]}, roots=[a], sent=sent)
    assert sent == [], "the backend was called with another project's file"
    assert "workspace root" in str(e.value)


@posix_only
def test_a_file_in_the_sessions_own_project_still_reaches_the_model(tmp_path):
    """The negative control: narrowing refuses what is outside, not everything."""
    a, _ = projects(tmp_path)
    sent: list = []
    call(files_cfg(tmp_path), "delegate_readonly",
         {"task": "read it", "files": [str(a / "x.py")]}, roots=[a], sent=sent)
    assert sent, "a file inside the session's own folder was refused"


@posix_only
@pytest.mark.parametrize(("tool", "arg"), [
    ("delegate", "workdir"),
    ("delegate_to_agent_readonly", "project"),
    ("list_agents", "project"),
])
def test_another_project_is_refused_as_a_workdir_or_a_project(tmp_path, tool, arg):
    """`workdir` is bound read-write into the sandbox and `project` is read for agent files:
    both were checked against the build-time roots, so both must see the session's."""
    a, b = projects(tmp_path)
    args: dict = {arg: str(b)}
    if tool != "list_agents":
        args["task"] = "work there"
    if tool == "delegate_to_agent_readonly":
        args["agent_name"] = "any"
    sent: list = []
    with pytest.raises(Exception, match="outside every workdir root"):
        call(files_cfg(tmp_path), tool, args, roots=[a], sent=sent)
    assert sent == []


@posix_only
def test_a_client_root_wider_than_the_ceiling_is_narrowed_to_the_ceiling(tmp_path):
    """A session opened above the configured roots gets the roots, never the folder."""
    a, b = projects(tmp_path)
    config = files_cfg(tmp_path, workspace_roots=(os.path.realpath(a),))
    with pytest.raises(Exception, match="workspace root"):
        call(config, "delegate_readonly",
             {"task": "read it", "files": [str(b / "x.py")]}, roots=[tmp_path])
    sent: list = []
    call(config, "delegate_readonly",
         {"task": "read it", "files": [str(a / "x.py")]}, roots=[tmp_path], sent=sent)
    assert sent


@posix_only
def test_a_client_whose_roots_are_all_outside_the_ceiling_is_refused(tmp_path):
    """Fails closed: a session that names only folders the operator never allowed gets
    nothing, rather than falling back to everything the operator did allow."""
    a, b = projects(tmp_path)
    config = files_cfg(tmp_path, workspace_roots=(os.path.realpath(a),))
    sent: list = []
    with pytest.raises(Exception, match="DELEGATE_WORKSPACE_ROOTS") as e:
        call(config, "delegate_readonly",
             {"task": "read it", "files": [str(a / "x.py")]}, roots=[b], sent=sent)
    assert sent == []
    assert str(os.path.realpath(b)) in str(e.value)


@posix_only
def test_a_client_that_lists_no_roots_falls_back_to_the_configured_ones(tmp_path):
    """The default: a client without roots support has not said where it works, so the
    operator's list applies as it always did."""
    _, b = projects(tmp_path)
    sent: list = []
    call(files_cfg(tmp_path), "delegate_readonly",
         {"task": "read it", "files": [str(b / "x.py")]}, roots=None, sent=sent)
    assert sent


@posix_only
def test_require_refuses_a_client_that_lists_no_roots(tmp_path):
    _, b = projects(tmp_path)
    sent: list = []
    with pytest.raises(Exception, match="DELEGATE_CLIENT_ROOTS"):
        call(files_cfg(tmp_path, client_roots="require"), "delegate_readonly",
             {"task": "read it", "files": [str(b / "x.py")]}, roots=None, sent=sent)
    assert sent == []


def test_a_misspelt_mode_is_refused_at_load_rather_than_read_as_narrow():
    from claude_delegate_local.config import Config, ConfigError

    with pytest.raises(ConfigError, match="DELEGATE_CLIENT_ROOTS"):
        Config(workspace_roots=(".",), client_roots="requre")


def test_narrowing_keeps_the_deeper_of_two_nested_roots_and_drops_disjoint_ones():
    from claude_delegate_local.paths import narrow_roots

    ceiling = ("/w/projects",)
    assert narrow_roots(ceiling, ["/w/projects/a"]) == ("/w/projects/a",)
    assert narrow_roots(ceiling, ["/w"]) == ("/w/projects",)
    assert narrow_roots(ceiling, ["/w/projects"]) == ("/w/projects",)
    assert narrow_roots(ceiling, ["/w/projectsX"]) == (), "a shared prefix is not nesting"
    assert narrow_roots(ceiling, ["/elsewhere"]) == ()
    assert narrow_roots(("/w/a", "/w/b"), ["/w"]) == ("/w/a", "/w/b")


@pytest.mark.parametrize(("uri", "expected"), [
    ("file:///C:/Users/x", "/mnt/c/Users/x"),
    ("file:///c%3A/Users/my%20dir", "/mnt/c/Users/my dir"),
    ("file:///home/dev/x", "/home/dev/x"),
    ("file:///home/dev/my%20dir", "/home/dev/my dir"),
])
def test_a_root_uri_becomes_the_posix_path_it_names(uri, expected):
    from claude_delegate_local.wsl import file_uri_to_posix

    assert file_uri_to_posix(uri) == expected


@pytest.mark.parametrize("uri", [
    "https://example.com/x",        # not a folder at all
    "file://server/share/x",        # a network share, which the server cannot reach
    "file:",                        # names nothing
    "file:///C:foo",                # drive-relative: would resolve against the server's cwd
])
def test_a_root_uri_that_names_no_reachable_folder_is_dropped(uri):
    from claude_delegate_local.wsl import file_uri_to_posix

    assert file_uri_to_posix(uri) is None
