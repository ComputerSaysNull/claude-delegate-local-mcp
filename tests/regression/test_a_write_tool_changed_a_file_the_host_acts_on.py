"""The write tools could change files a host program acts on (PLAN M13.7, review R3).

`write_file` and `edit_file` go through the path policy, which refused secrets and
gitignored files but not tracked configuration: Claude Code's own `.claude/` settings,
agents and skills, `CLAUDE.md`, editor task files. A change there takes effect on the host
before anyone reads a diff -- hooks apply mid-session -- so a delegation steered by the
content it was reading could rewrite the rules the operator's own session runs under.

The fix is a list, `security/protected_globs.txt`, refused by name to both write tools
whether or not the file exists yet. Reading stays allowed: the point is who may change
these files, not who may see them. The sandbox half is a separate layer with its own test.
The design is docs/specs/2026-09-23-host-acted-paths.md.

`edit_file` is a case of its own because it opens an *existing* file, so a rule keyed on
"this call may create a file" would have missed it.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="LAYER 1 UNPROVEN BY THIS RUN -- run it under WSL, where the server lives.",
)


def cfg(root: Path) -> Config:
    return Config(workspace_roots=(str(root),), respect_gitignore=False)  # type: ignore[arg-type]


def _call(root: Path, name: str, **args):
    call = ToolUseBlock(id="call-1", name=name, input=dict(args))
    return tools.execute_tool(cfg(root), call, tools.ALL_TOOL_NAMES)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "notes.md").write_text("notes\n", encoding="utf-8")
    return tmp_path


PROTECTED = [
    ".claude/settings.json",          # exists
    ".claude/settings.local.json",    # does not exist yet
    "CLAUDE.md",                      # exists
    "sub/CLAUDE.md",                  # a nested one, not yet there
    "CLAUDE.local.md",
    ".mcp.json",
]


@posix_only
@pytest.mark.parametrize("rel", PROTECTED)
def test_write_file_refuses_a_host_acted_path(project, rel):
    target = project / rel
    before = target.read_bytes() if target.exists() else None
    result = _call(project, "write_file", path=str(target), content="planted\n")
    assert result.is_error, result.content
    assert "protected" in result.content, result.content
    after = target.read_bytes() if target.exists() else None
    assert after == before, f"{rel} was changed"


@posix_only
@pytest.mark.parametrize("rel", [".claude/settings.json", "CLAUDE.md"])
def test_edit_file_refuses_a_host_acted_path(project, rel):
    target = project / rel
    before = target.read_bytes()
    old = before.decode().strip()
    result = _call(project, "edit_file", path=str(target), old_string=old,
                   new_string="planted")
    assert result.is_error, result.content
    assert "protected" in result.content, result.content
    assert target.read_bytes() == before


@posix_only
def test_an_ordinary_file_is_still_writable(project):
    """The control: the layer is a list, not a lock on the project."""
    result = _call(project, "write_file", path=str(project / "notes.md"), content="new\n")
    assert not result.is_error, result.content
    assert (project / "notes.md").read_text(encoding="utf-8") == "new\n"


@posix_only
def test_a_protected_file_is_still_readable(project):
    """Who may change these files, not who may see them."""
    result = _call(project, "read_file", path=str(project / "CLAUDE.md"))
    assert not result.is_error, result.content
    assert "# rules" in result.content
