"""`search_files` reported absent what `read_file` would show (review R12, PLAN M13.5).

The extension allowlist mixes suffixes (`.py`) with whole filenames written with a leading
dot (`.makefile`, `.gitignore`). `read_file` judges a suffix-less name by the whole name, so
`Makefile` is readable. The search walk re-derived its own set and tested `splitext` alone,
which is empty for `Makefile` and for `.gitignore`, so neither was ever searched: a search
for text a read would show answered "no matches".

The fix is one predicate for both, `paths.extension_refusal`, so the two cannot disagree
again. Named after the bug, per the project's convention.
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


# The tool's own words for an empty result. Asserting on a file's *name* cannot fail: the
# empty answer lists the root's entries as places to scope to, so a name appears in it
# whether or not the file was searched. That shape passed against the unfixed code.
NO_MATCH = "No line matched"


def _run(root: Path, name: str, **args) -> str:
    call = ToolUseBlock(id="call-1", name=name, input=dict(args))
    result = tools.execute_tool(cfg(root), call, tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    return result.content


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "Makefile").write_text("build:\n\techo needle-in-makefile\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("needle-in-gitignore/\n", encoding="utf-8")
    (tmp_path / "core.py").write_text("NEEDLE = 'needle-in-source'\n", encoding="utf-8")
    return tmp_path


@posix_only
@pytest.mark.parametrize("name, needle", [
    ("Makefile", "needle-in-makefile"),
    (".gitignore", "needle-in-gitignore"),
])
def test_a_suffixless_name_read_file_allows_is_searched(tree, name, needle):
    """The route: the same file, both tools. A read shows it, so a search must find it."""
    assert needle in _run(tree, "read_file", path=str(tree / name))
    out = _run(tree, "search_files", pattern=needle, path=str(tree))
    assert NO_MATCH not in out, out
    assert name in out, out


@posix_only
def test_a_suffixless_name_off_the_allowlist_is_still_skipped(tree):
    """The control. Widening to whole names must not widen past the allowlist: a file named
    `LICENSE` has no allowlisted suffix and no allowlisted name, so `read_file` refuses it
    and the search must not return it."""
    (tree / "LICENSE").write_text("needle-in-license\n", encoding="utf-8")
    out = _run(tree, "search_files", pattern="needle-in-license", path=str(tree))
    assert NO_MATCH in out, out


@posix_only
def test_a_suffixed_file_is_found_either_way(tree):
    """The case that always worked, so a fix that broke suffix matching fails here."""
    out = _run(tree, "search_files", pattern="needle-in-source", path=str(tree))
    assert NO_MATCH not in out, out
    assert "core.py" in out, out
