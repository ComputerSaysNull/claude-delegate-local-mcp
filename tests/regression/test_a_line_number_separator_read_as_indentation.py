"""A `read_file` line-number separator was read back as the file's own indentation.

`read_file` and prefetch print each line as `<number>  <text>` -- two spaces. A model
quoting a line for `edit_file` often keeps those two spaces, so its `old_string` is
over-indented and misses. The separator is now a tab, a character a file rarely indents
with, and a hint names the prefix so the model drops it instead of resending the same
edit.

The pure helper is tested directly because layer 1 needs a real POSIX filesystem; the
end-to-end refusals carry the same skip marker the existing `edit_file` tests use.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config
from claude_delegate_local.context import numbered_line

UNPROVEN = (
    "LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Resolving a real path needs a "
    "POSIX filesystem and the server runs in WSL, so every case that actually opens a "
    "file is proven there. Run: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest "
    "tests/regression/test_a_line_number_separator_read_as_indentation.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


def test_the_separator_after_the_line_number_is_a_tab():
    rendered = numbered_line(3, "    x = 1", 1)
    assert rendered.startswith("3\t")
    assert not rendered.lstrip("0123456789").startswith(" "), (
        "the separator is two spaces, which a model copies back as file indentation"
    )


@posix_only
def test_edit_file_refusal_names_the_separator(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("    x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="\t    x = 1",
             new_string="\t    x = 2"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "does not appear" in result.content
    assert "separator" in result.content


@posix_only
def test_edit_file_refusal_names_the_line_numbers(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("    x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="3\t    x = 1",
             new_string="3\t    x = 2"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "does not appear" in result.content
    assert "line number" in result.content


@posix_only
def test_a_tab_that_is_not_a_prefix_gets_no_prefix_hint(tmp_path):
    target = tmp_path / "Makefile"
    target.write_text("all:\n\tcc -o x x.c\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="\tcc -o y x.c",
             new_string="\tcc -o z x.c"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "read_file" not in result.content


@posix_only
def test_lines_found_one_by_one_but_not_together_get_no_prefix_hint(tmp_path):
    # Each line minus its tab is in the file, but not in this order, so the tab is not
    # the reason the quote missed.
    target = tmp_path / "mod.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="\tb = 2\n\ta = 1",
             new_string="\tb = 3\n\ta = 1"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "separator" not in result.content


@posix_only
def test_a_correct_quote_of_a_tab_indented_makefile_line_still_succeeds(tmp_path):
    target = tmp_path / "Makefile"
    target.write_text("all:\n\tcc -o x x.c\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="\tcc -o x x.c",
             new_string="\tcc -o y x.c"),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error
    assert "Replaced 1 occurrence" in result.content
