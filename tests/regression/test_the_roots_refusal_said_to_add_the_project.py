"""The refusal for a session outside the project ceiling said to add *the project*.

The roots are a ceiling (ADR-0110), so the fix is one parent folder, set once: adding each
project only meant editing `.env` again at the next one. And the refusal reaches a session
in another project, which cannot know where this server's `.env` lives, so it now names
the file the settings were read from.

The project is not a root the client lists any more: it is `Config.project_dir`, the folder
`CLAUDE_PROJECT_DIR` names (ADR-0128), and the server no longer reads MCP roots. A session
whose project falls outside the ceiling is refused the same way, still asking for the parent
folder rather than the project, and still naming the env file the settings came from.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from claude_delegate_local import config, server
from test_server import DoubleCache, answered, entry, files_cfg, recording_handler, registry


def refusal(cfg) -> str:
    """The text of the refusal a session with this config gets.

    The client lists no roots: the server no longer reads them, so the session's project
    comes entirely from `cfg.project_dir`.
    """
    mcp = server.build(cfg, registry(entry()), DoubleCache(cfg, recording_handler([])))

    async def go():
        async with Client(mcp) as client:
            await answered(client, "delegate_readonly", {"task": "t", "effort": "inherit"})

    with pytest.raises(Exception) as e:  # fastmcp re-raises its own error type
        asyncio.run(go())
    return str(e.value)


def outside(tmp_path: Path) -> tuple[Path, Path]:
    ceiling, elsewhere = tmp_path / "projects", tmp_path / "elsewhere"
    for p in (ceiling, elsewhere):
        p.mkdir()
    return ceiling, elsewhere


def test_the_refusal_asks_for_the_parent_folder_not_the_project(tmp_path):
    ceiling, elsewhere = outside(tmp_path)
    text = refusal(files_cfg(ceiling, project_dir=os.path.realpath(elsewhere)))
    assert "the folder your projects live in" in text
    assert "add the project" not in text


def test_the_refusal_names_the_env_file_the_settings_came_from(tmp_path):
    ceiling, elsewhere = outside(tmp_path)
    env = tmp_path / "server.env"
    text = refusal(replace(
        files_cfg(ceiling, project_dir=os.path.realpath(elsewhere)), env_file=str(env)))
    assert str(env) in text


def test_the_loader_records_the_env_file_it_read(tmp_path):
    env = tmp_path / "server.env"
    env.write_text(f"DELEGATE_WORKSPACE_ROOTS={tmp_path}\n", encoding="utf-8")
    assert config.load(env_file=env).env_file == str(env)


def test_no_env_file_read_records_none():
    """The negative control: a loader that always named a file would pass the test above."""
    assert config.load(environ={"DELEGATE_WORKSPACE_ROOTS": "/anywhere"}).env_file is None
