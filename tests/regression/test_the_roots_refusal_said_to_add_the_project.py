"""The refusal for a session outside the roots said to add *the project*.

The roots are a ceiling (ADR-0110), so the fix is one parent folder, set once: adding each
project only meant editing `.env` again at the next one. And the refusal reaches a session
in another project, which cannot know where this server's `.env` lives, so it now names
the file the settings were read from.
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

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="the path policy resolves POSIX paths; the server is POSIX-only")


def refusal(cfg, roots: list[Path]) -> str:
    """The text of the refusal a session listing only `roots` gets."""
    mcp = server.build(cfg, registry(entry()), DoubleCache(cfg, recording_handler([])))

    async def go():
        async with Client(mcp, roots=[r.as_uri() for r in roots]) as client:
            await answered(client, "delegate_readonly", {"task": "t", "effort": "inherit"})

    with pytest.raises(Exception) as e:  # fastmcp re-raises its own error type
        asyncio.run(go())
    return str(e.value)


def outside(tmp_path: Path) -> tuple[Path, Path]:
    ceiling, elsewhere = tmp_path / "projects", tmp_path / "elsewhere"
    for p in (ceiling, elsewhere):
        p.mkdir()
    return ceiling, elsewhere


@posix_only
def test_the_refusal_asks_for_the_parent_folder_not_the_project(tmp_path):
    ceiling, elsewhere = outside(tmp_path)
    text = refusal(files_cfg(ceiling), [elsewhere])
    assert "the folder your projects live in" in text
    assert "add the project" not in text


@posix_only
def test_the_refusal_names_the_env_file_the_settings_came_from(tmp_path):
    ceiling, elsewhere = outside(tmp_path)
    env = tmp_path / "server.env"
    text = refusal(replace(files_cfg(ceiling), env_file=str(env)), [elsewhere])
    assert str(env) in text


def test_the_loader_records_the_env_file_it_read(tmp_path):
    env = tmp_path / "server.env"
    env.write_text(f"DELEGATE_WORKSPACE_ROOTS={tmp_path}\n", encoding="utf-8")
    assert config.load(env_file=env).env_file == str(env)


def test_no_env_file_read_records_none():
    """The negative control: a loader that always named a file would pass the test above."""
    assert config.load(environ={"DELEGATE_WORKSPACE_ROOTS": "/anywhere"}).env_file is None
