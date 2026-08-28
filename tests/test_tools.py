"""The model-facing tools, and both places `allowed_tools` is enforced.

The enforcement tests matter more than the tool tests. Filtering the declared list is
advisory — a model can call a tool it was never offered — so the execution site is the
one doing the work, and a test that only checks an error came back would pass against a
handler that ran first and failed afterwards. The call-counting stub below is what
distinguishes "refused" from "ran and then complained".

Layers 1 and 4 of the path policy need a real POSIX filesystem, so the cases that turn on
them are proven under WSL rather than here; see tests/test_paths.py for the same split.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config


UNPROVEN = (
    "LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Resolving a real path needs a "
    "POSIX filesystem and the server runs in WSL, so every case that actually opens a "
    "file is proven there. Run: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/cdl/bin/python -m pytest tests/test_tools.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


# --- the declaration site ---------------------------------------------------------------


def test_declared_tools_returns_only_the_permitted_ones():
    names = [s.name for s in tools.declared_tools({"read_file"})]
    assert names == ["read_file"]


def test_declared_tools_returns_nothing_for_an_empty_set():
    assert tools.declared_tools(set()) == ()


def test_declared_order_is_the_registry_order_not_the_callers():
    """Schemas sit in the cached prefix (ADR-0011), so the same set must render the same."""
    forward = [s.name for s in tools.declared_tools(["read_file", "write_file"])]
    backward = [s.name for s in tools.declared_tools(["write_file", "read_file"])]
    assert forward == backward == ["read_file", "write_file"]


def test_every_registered_tool_has_a_description_and_a_schema():
    """The description is the model-facing contract; an empty one is a silent regression."""
    for name, tool in tools.REGISTRY.items():
        assert tool.spec.name == name
        assert len(tool.spec.description) > 40, name
        assert tool.spec.input_schema.get("properties"), name


# --- the execution site -----------------------------------------------------------------


def test_a_tool_outside_the_allowed_set_never_reaches_its_handler(workspace, monkeypatch):
    """The test the whole two-site rule exists for.

    Asserting only that an error came back would pass against an implementation that ran
    the handler and then reported the refusal -- by which point the file is already read.
    """
    ran = []
    monkeypatch.setitem(
        tools.REGISTRY, "read_file",
        tools.RegisteredTool(
            spec=tools.REGISTRY["read_file"].spec,
            handler=lambda c, a: ran.append(1) or "should not happen"),
    )
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py")), {"write_file"})
    assert result.is_error
    assert ran == [], "the handler ran despite the tool being disallowed"


@posix_only
def test_an_allowed_tool_does_reach_its_handler(workspace):
    """Without this, refusing everything would pass the test above."""
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py")),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error
    assert result.content == "x = 1\n"


def test_an_unknown_tool_name_is_an_error_not_a_crash(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("delete_everything", path="/"), {"delete_everything"})
    assert result.is_error
    assert "not a tool" in result.content


def test_a_refusal_is_an_error_result_and_keeps_the_call_id(workspace):
    """Mid-loop a refusal must not end the delegation, and must be attributable."""
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "missing.py")),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert result.tool_use_id == "call-1"


# --- read_file --------------------------------------------------------------------------


@posix_only
def test_read_file_returns_the_whole_file_when_it_fits(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py")),
        tools.ALL_TOOL_NAMES)
    assert result.content == "x = 1\n"


@posix_only
def test_read_file_pages_and_reports_the_true_total(workspace):
    (workspace / "big.py").write_text("abcdefghij" * 10, encoding="utf-8")
    c = cfg(workspace, max_read_chars=30)
    first = tools.execute_tool(
        c, call("read_file", path=str(workspace / "big.py")), tools.ALL_TOOL_NAMES)
    assert not first.is_error
    assert "characters 0 to 30 of 100" in first.content
    assert "offset=30" in first.content


@posix_only
def test_read_file_continues_from_the_offset_it_reported(workspace):
    (workspace / "big.py").write_text("abcdefghij" * 10, encoding="utf-8")
    c = cfg(workspace, max_read_chars=30)
    tail = tools.execute_tool(
        c, call("read_file", path=str(workspace / "big.py"), offset=90),
        tools.ALL_TOOL_NAMES)
    assert tail.content == "abcdefghij"
    assert "truncated" not in tail.content


@posix_only
def test_read_file_refuses_an_offset_past_the_end(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py"), offset=999),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "past the end" in result.content


@posix_only
def test_read_file_refuses_a_binary_file(workspace):
    (workspace / "b.py").write_bytes(b"\x00\x01\x02")
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "b.py")),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "not text" in result.content


def test_read_file_refuses_a_non_string_path(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=17), tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "must be a string" in result.content


# --- write_file -------------------------------------------------------------------------


@posix_only
def test_write_file_creates_a_new_file(workspace):
    target = workspace / "new.py"
    result = tools.execute_tool(
        cfg(workspace), call("write_file", path=str(target), content="y = 2\n"),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    assert target.read_text(encoding="utf-8") == "y = 2\n"
    assert "Created" in result.content


@posix_only
def test_write_file_overwrites_and_says_so(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("write_file", path=str(workspace / "a.py"), content="z = 3\n"),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error
    assert "Overwrote" in result.content
    assert (workspace / "a.py").read_text(encoding="utf-8") == "z = 3\n"


def test_write_file_refuses_rather_than_truncating(workspace):
    """A silently shortened file is a corrupted one the model cannot detect."""
    target = workspace / "big.py"
    result = tools.execute_tool(
        cfg(workspace, max_write_bytes=10),
        call("write_file", path=str(target), content="x" * 50), tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "exceeds" in result.content
    assert not target.exists(), "a refused write must not have happened partially"


@posix_only
def test_write_file_refuses_a_missing_parent_directory(workspace):
    """must_exist=False relaxes the file, never the tree above it."""
    result = tools.execute_tool(
        cfg(workspace),
        call("write_file", path=str(workspace / "nope" / "x.py"), content="a = 1\n"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "does not exist" in result.content


@posix_only
def test_write_file_refuses_an_existing_directory(workspace):
    (workspace / "sub").mkdir()
    result = tools.execute_tool(
        cfg(workspace), call("write_file", path=str(workspace / "sub"), content="a = 1\n"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error


def test_write_file_still_refuses_an_unlisted_extension(workspace):
    """Relaxing existence must not have relaxed any other layer."""
    result = tools.execute_tool(
        cfg(workspace), call("write_file", path=str(workspace / "x.exe"), content="a"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error


# --- run_bash ---------------------------------------------------------------------------


def test_run_bash_refuses_every_call(workspace):
    """ADR-0010: no sandbox means no shell, not an unconfined shell."""
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="echo hi"), tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "ADR-0010" in result.content


def test_run_bash_refusal_names_the_sandbox_as_the_reason(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="ls"), tools.ALL_TOOL_NAMES)
    assert "sandbox" in result.content.lower()
    assert "unconfined" in result.content.lower()


def test_run_bash_does_not_consult_the_still_inert_sandbox_settings(workspace):
    """Reading either would mark it live in the generated reference while nothing acts.

    Asserted by behaviour rather than by inspection: turning the sandbox off in config must
    not turn the refusal into an unconfined run, because there is no such branch.
    """
    result = tools.execute_tool(
        cfg(workspace, sandbox_enabled=False), call("run_bash", command="echo hi"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
