"""`run_bash` never said that `.git` is hidden, so a model ran `git show` there and failed.

Only `read_git`'s description said the shell cannot see `.git`, and a model reaching for a
shell reads the shell's description, not the other tool's. Every git command under
`run_bash` exits 128 against the empty mount, without saying why.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local.tools import RUN_BASH


def test_run_bash_says_git_is_hidden_and_where_to_go_instead():
    text = RUN_BASH.spec.description
    assert ".git" in text
    assert "read_git" in text
