"""A session could read another project's workspace.

Every session's server read one `DELEGATE_WORKSPACE_ROOTS`, so a session opened in project B
could read project A whenever both were listed -- and listing both is what an operator
hopping between projects ends up doing, because editing the list on every switch is the
alternative. So each session is narrowed to its own project, and the configured list is a
ceiling rather than a grant.

The project used to come from MCP roots (ADR-0110). Roots are deprecated and FastMCP 4
removed the call, so it now comes from `CLAUDE_PROJECT_DIR`, which Claude Code sets for every
stdio server it starts (ADR-0128). A client's roots are no longer read at all: a client that
lists project B as a root still reaches only the project it was started in.
"""

import asyncio
import os
from pathlib import Path

import pytest
from fastmcp import Client

from claude_delegate_local import config, server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry


def projects(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        p.mkdir()
        (p / "x.py").write_text("x = 1\n", encoding="utf-8")
    return a, b


def call(cfg, tool: str, args: dict, *, roots=None, sent=None) -> dict:
    """One tool call over a real MCP session whose client lists `roots`, or none at all."""
    sent = [] if sent is None else sent
    mcp = server.build(cfg, registry(entry()), DoubleCache(cfg, recording_handler(sent)))
    if tool.startswith("delegate"):
        args.setdefault("effort", "inherit")

    async def go():
        kw = {} if roots is None else {"roots": [Path(r).as_uri() for r in roots]}
        async with Client(mcp, **kw) as client:
            return await answered(client, tool, args)

    return asyncio.run(go())


def in_project(tmp_path: Path, project: Path | None, **over) -> config.Config:
    """`files_cfg` under `require`, the default, as a server started in `project` loads it."""
    return files_cfg(tmp_path, client_roots="require",
                     project_dir=os.path.realpath(project) if project else None, **over)


def test_the_folder_claude_code_names_is_the_sessions_project(tmp_path):
    """End to end, through `config.load`: the variable Claude Code sets is what is read."""
    a, _ = projects(tmp_path)
    globs = tmp_path / "globs.txt"
    globs.write_text(".env\n", encoding="utf-8")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    loaded = config.load(environ={
        "DELEGATE_WORKSPACE_ROOTS": os.path.realpath(tmp_path),
        "DELEGATE_SECRET_GLOBS_FILE": str(globs),
        "DELEGATE_RESPECT_GITIGNORE": "false",
        "DELEGATE_ADMISSION_IDLE_HOLD": "0",
        "DELEGATE_CONTEXT_OVERFLOW_ENABLED": "false",
        "CLAUDE_PROJECT_DIR": str(a),
    }, env_file=empty_env)
    assert loaded.client_roots == "require", "the default must be what is under test"
    sent: list = []
    call(loaded, "delegate_readonly", {"task": "read it", "files": [str(a / "x.py")]},
         sent=sent)
    assert sent, "a file in the session's own project was refused"


def test_a_file_in_another_project_is_refused(tmp_path):
    a, b = projects(tmp_path)
    sent: list = []
    with pytest.raises(Exception) as e:  # fastmcp re-raises its own error type
        call(in_project(tmp_path, a), "delegate_readonly",
             {"task": "read it", "files": [str(b / "x.py")]}, sent=sent)
    assert sent == [], "the backend was called with another project's file"
    assert "workspace root" in str(e.value)


def test_a_client_listing_another_project_as_a_root_still_does_not_reach_it(tmp_path):
    """Roots are not read any more, so they can neither narrow nor widen a session."""
    a, b = projects(tmp_path)
    sent: list = []
    with pytest.raises(Exception, match="workspace root"):
        call(in_project(tmp_path, a), "delegate_readonly",
             {"task": "read it", "files": [str(b / "x.py")]}, roots=[a, b], sent=sent)
    assert sent == []


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
        call(in_project(tmp_path, a), tool, args, sent=sent)
    assert sent == []


def test_a_project_wider_than_the_ceiling_is_narrowed_to_the_ceiling(tmp_path):
    """A session started above the configured roots gets the roots, never the folder."""
    a, b = projects(tmp_path)
    cfg = in_project(tmp_path, tmp_path, workspace_roots=(os.path.realpath(a),))
    with pytest.raises(Exception, match="workspace root"):
        call(cfg, "delegate_readonly", {"task": "read it", "files": [str(b / "x.py")]})
    sent: list = []
    call(cfg, "delegate_readonly", {"task": "read it", "files": [str(a / "x.py")]}, sent=sent)
    assert sent


def test_a_project_outside_the_ceiling_is_refused(tmp_path):
    """Fails closed: a session started in a folder the operator never allowed gets
    nothing, rather than falling back to everything the operator did allow."""
    a, b = projects(tmp_path)
    cfg = in_project(tmp_path, b, workspace_roots=(os.path.realpath(a),))
    sent: list = []
    with pytest.raises(Exception, match="DELEGATE_WORKSPACE_ROOTS") as e:
        call(cfg, "delegate_readonly", {"task": "read it", "files": [str(a / "x.py")]},
             sent=sent)
    assert sent == []
    assert str(os.path.realpath(b)) in str(e.value)


def test_narrow_gives_a_server_without_a_project_the_configured_roots(tmp_path):
    """`narrow`: a server started without `CLAUDE_PROJECT_DIR` has not been told where it
    works, so the operator's list applies as it always did. Not the default (ADR-0119)."""
    _, b = projects(tmp_path)
    sent: list = []
    call(files_cfg(tmp_path, client_roots="narrow"), "delegate_readonly",
         {"task": "read it", "files": [str(b / "x.py")]}, sent=sent)
    assert sent


def test_require_refuses_a_server_without_a_project(tmp_path):
    _, b = projects(tmp_path)
    sent: list = []
    with pytest.raises(Exception, match="CLAUDE_PROJECT_DIR"):
        call(in_project(tmp_path, None), "delegate_readonly",
             {"task": "read it", "files": [str(b / "x.py")]}, roots=[b], sent=sent)
    assert sent == []


def load_with(tmp_path: Path, live: dict[str, str], env_text: str = "") -> config.Config:
    env = tmp_path / "settings.env"
    env.write_text(env_text, encoding="utf-8")
    return config.load(environ={"DELEGATE_WORKSPACE_ROOTS": str(tmp_path), **live},
                       env_file=env)


def test_the_operators_env_file_cannot_name_the_project(tmp_path):
    """The project is the client's to name: a `.env` line setting it is ignored."""
    a, _ = projects(tmp_path)
    assert load_with(tmp_path, {}, f"CLAUDE_PROJECT_DIR={a}\n").project_dir is None
    assert load_with(tmp_path, {}, f"DELEGATE_PROJECT_DIR={a}\n").project_dir is None
    assert load_with(tmp_path, {"DELEGATE_PROJECT_DIR": str(a)}).project_dir is None
    assert load_with(tmp_path, {"CLAUDE_PROJECT_DIR": str(a)}).project_dir == str(a.resolve())


def test_a_relative_project_is_refused_rather_than_resolved_against_the_cwd(tmp_path):
    with pytest.raises(config.ConfigError, match="CLAUDE_PROJECT_DIR"):
        load_with(tmp_path, {"CLAUDE_PROJECT_DIR": "some/project"})


def test_a_windows_form_project_crosses_the_boundary_like_any_client_path(tmp_path):
    loaded = load_with(tmp_path, {"CLAUDE_PROJECT_DIR": r"C:\Users\me\proj"})
    assert loaded.project_dir == os.path.realpath("/mnt/c/Users/me/proj")


def test_a_project_outside_the_workdir_roots_gets_no_workdir_but_still_reads(tmp_path):
    """Configured `workdir_roots` that exclude the project: no `workdir`, never a fallback to
    the configured ones, while reading inside the project is unaffected."""
    a, b = projects(tmp_path)
    cfg = in_project(tmp_path, a, workdir_roots=(os.path.realpath(b),))
    sent: list = []
    with pytest.raises(Exception, match="CLAUDE_PROJECT_DIR"):
        call(cfg, "delegate", {"task": "work there", "workdir": str(b)}, sent=sent)
    assert sent == []
    call(cfg, "delegate_readonly", {"task": "read it", "files": [str(a / "x.py")]}, sent=sent)
    assert sent


def test_a_misspelt_mode_is_refused_at_load_rather_than_read_as_narrow():
    with pytest.raises(config.ConfigError, match="DELEGATE_CLIENT_ROOTS"):
        config.Config(workspace_roots=(".",), client_roots="requre")


def test_narrowing_keeps_the_deeper_of_two_nested_roots_and_drops_disjoint_ones():
    from claude_delegate_local.paths import narrow_roots

    ceiling = ("/w/projects",)
    assert narrow_roots(ceiling, ["/w/projects/a"]) == ("/w/projects/a",)
    assert narrow_roots(ceiling, ["/w"]) == ("/w/projects",)
    assert narrow_roots(ceiling, ["/w/projects"]) == ("/w/projects",)
    assert narrow_roots(ceiling, ["/w/projectsX"]) == (), "a shared prefix is not nesting"
    assert narrow_roots(ceiling, ["/elsewhere"]) == ()
    assert narrow_roots(("/w/a", "/w/b"), ["/w"]) == ("/w/a", "/w/b")
