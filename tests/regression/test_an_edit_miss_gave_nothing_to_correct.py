"""A zero-match `edit_file` names the nearest region instead of sending the model back.

The miss used to be a dead end: one delegation re-read the lines and sent the byte-identical
edit again, because the refusal said only "it does not appear" with nothing to correct
against. The hint points at the closest line and the first difference -- as line numbers, a
column, and code points, never as file text. The refusal is recorded in the operator
transcript, and ADR-0039 keeps file contents out of that record, so a hint that quoted the
file would put source into a log that must not hold it.

The pure helper is tested directly because layer 1 needs a real POSIX filesystem; the
end-to-end refusal carries the same skip marker the existing `edit_file` tests use.
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
    "tests/regression/test_an_edit_miss_gave_nothing_to_correct.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


# (a) a tab in the file where old_string has spaces
def test_the_hint_names_a_tab_in_place_of_spaces():
    file_text = "def one():\n\treturn 1\n"
    old = "def one():\n    return 1"
    hint = tools._nearest_miss(file_text, old)
    assert hint is not None
    assert "starts at line 1" in hint
    assert "line 2, column 1" in hint
    assert "U+0009 TAB" in hint
    assert "U+0020 SPACE" in hint


# (b) a curly apostrophe in the file where old_string has the ASCII one
def test_the_hint_names_a_curly_apostrophe_in_place_of_ascii():
    file_text = "the user\u2019s name\n"
    old = "the user's name"
    hint = tools._nearest_miss(file_text, old)
    assert hint is not None
    assert "column 9" in hint
    assert "U+2019 RIGHT SINGLE QUOTATION MARK" in hint
    assert "U+0027 APOSTROPHE" in hint


# (c) nothing close -> the refusal stays as today
def test_no_close_line_returns_none():
    assert tools._nearest_miss("alpha\nbeta\n", "xyzzy\nzzzz") is None


# (d) ADR-0039: a hint recorded in the transcript must not carry file contents
def test_the_hint_never_quotes_file_text():
    file_text = "zeta-line-one\nzeta-line-two\nzeta-line-three\n"
    old = "zeta-line-one\nzeta-line-two-X"
    hint = tools._nearest_miss(file_text, old)
    assert hint is not None
    for line in file_text.splitlines():
        assert line not in hint
    for line in old.splitlines():
        assert line not in hint


# (e) end-to-end: a near-miss edit_file refusal carries the hint
@posix_only
def test_edit_file_refusal_names_the_nearest_region(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("def one():\n\treturn 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="def one():\n    return 1",
             new_string="def one():\n    return 2"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "does not appear" in result.content
    assert "closest match starts at line" in result.content
