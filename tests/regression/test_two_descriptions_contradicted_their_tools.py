"""Two tool descriptions said something their tools no longer do.

`run_bash` said only that `$DELEGATE_PYTHON` runs the tests, so three calls reached for a
bare `ruff` and got exit 127: a project's own tools are no more on PATH than the
interpreter is. And `read_git` told the model never to pass `--`, while its own `args`
description, and `_split_separator` since 2026-09-23, accept one and treat what follows
as paths.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local.tools import READ_GIT, RUN_BASH


def test_run_bash_says_the_projects_tools_run_through_the_interpreter():
    text = RUN_BASH.spec.description
    assert '"$DELEGATE_PYTHON" -m ruff' in text


def test_read_git_no_longer_forbids_the_separator_it_accepts():
    assert "never pass '--'" not in READ_GIT.spec.description
