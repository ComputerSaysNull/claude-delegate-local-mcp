"""A `run_bash` with no workdir failed on the repository's files without saying why.

`write_file` and `edit_file` resolve against the workspace roots, so a writing delegation
with no `workdir` can write a file and then find its shell cannot see it: nothing of the
caller's is bound, and the command fails with "No such file or directory". The model was
told only that, and spent turns probing a path that exists everywhere except where it
looked. A failed command in an unbound shell now says so beside the output.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import sandbox, tools
from claude_delegate_local.config import Config

# The words, not a constant, so the red is an assertion rather than an AttributeError.
HINT = "sees none of your files"


def _cfg(tmp_path) -> Config:
    return Config(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home"))


def _run(tmp_path, monkeypatch, *, exit_code: int, workdir: str | None,
         masked: bool = False):
    monkeypatch.setattr(sandbox, "run", lambda c, r: sandbox.SandboxResult(
        stdout="", stderr="cat: src/foo.py: No such file or directory",
        exit_code=exit_code, timed_out=False, masked_failure=masked,
    ))
    return tools._run_bash(_cfg(tmp_path), {"command": "cat src/foo.py"},
                           tools.BashPolicy(workdir=workdir))


def test_a_failed_command_with_no_workdir_says_the_shell_sees_none_of_your_files(
        tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, exit_code=1, workdir=None)
    assert HINT in result.text


def test_a_masked_failure_with_no_workdir_says_it_too(tmp_path, monkeypatch):
    """`cat missing | head` exits 0; the failure it hides is the same missing file."""
    result = _run(tmp_path, monkeypatch, exit_code=0, workdir=None, masked=True)
    assert HINT in result.text


@pytest.mark.parametrize(("exit_code", "workdir"), [(0, None), (1, "bound")])
def test_the_note_stays_out_when_it_would_be_wrong(tmp_path, monkeypatch, exit_code, workdir):
    """Controls: a success needs no explanation, and a bound shell does see the files."""
    bound = str(tmp_path) if workdir else None
    result = _run(tmp_path, monkeypatch, exit_code=exit_code, workdir=bound)
    assert HINT not in result.text
