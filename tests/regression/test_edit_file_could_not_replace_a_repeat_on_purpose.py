"""`edit_file` could not replace a repeated `old_string` on purpose.

`_edit_file` counts `old_string` occurrences and refuses unless it appears exactly once,
so a deliberate "replace all three" was impossible without three calls or a run_bash sed.
The fix adds an optional integer `expected_count` (at least 1): given, the text must appear
exactly that many times and then ALL occurrences are replaced. Omitted means today's
behaviour exactly -- it must appear once.

The end-to-end cases carry the same skip marker the existing `edit_file` regression tests
use, because resolving a real path needs a POSIX filesystem and the server runs in WSL.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

UNPROVEN = (
    "LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Resolving a real path needs a "
    "POSIX filesystem and the server runs in WSL, so every case that actually opens a "
    "file is proven there. Run: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest "
    "tests/regression/test_edit_file_could_not_replace_a_repeat_on_purpose.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


@posix_only
def test_expected_count_replaces_every_occurrence(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2",
             expected_count=3),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error
    assert "Replaced 3 occurrences" in result.content
    assert target.read_text(encoding="utf-8") == "x = 2\nx = 2\nx = 2\n"


@posix_only
def test_wrong_expected_count_refuses_and_writes_nothing(tmp_path):
    target = tmp_path / "mod.py"
    original = "x = 1\nx = 1\nx = 1\n"
    target.write_text(original, encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2",
             expected_count=2),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "3" in result.content
    assert "2" in result.content
    assert "nothing was written" in result.content
    assert target.read_text(encoding="utf-8") == original


@posix_only
def test_expected_count_below_one_is_refused(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2",
             expected_count=0),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "expected_count" in result.content
    assert target.read_text(encoding="utf-8") == "x = 1\n"


@posix_only
def test_expected_count_as_a_string_is_refused(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2",
             expected_count="2"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "expected_count" in result.content
    assert target.read_text(encoding="utf-8") == "x = 1\n"


@posix_only
def test_without_expected_count_a_repeat_is_still_refused(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "appears 2 times" in result.content
    assert target.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_expected_count_is_declared_and_not_required():
    schema = tools.EDIT_FILE.spec.input_schema
    prop = schema["properties"]["expected_count"]
    assert prop["type"] == "integer"
    assert prop["minimum"] == 1
    assert "expected_count" not in schema["required"]
