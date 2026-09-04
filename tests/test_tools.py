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
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import sandbox, tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config


UNPROVEN = (
    "LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Resolving a real path needs a "
    "POSIX filesystem and the server runs in WSL, so every case that actually opens a "
    "file is proven there. Run: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest tests/test_tools.py'"
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
    assert result.content == "1  x = 1"


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
    """Numbered, and with no truncation footer. The trailing newline is not preserved --
    lines are split and rejoined so a CRLF file reads the same as an LF one, which makes
    this a numbered view of a file rather than a byte-exact copy of it."""
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py")),
        tools.ALL_TOOL_NAMES)
    assert result.content == "1  x = 1"


@posix_only
def test_read_file_refuses_a_file_over_the_byte_ceiling(workspace):
    """`max_file_read_bytes` calls itself "checked by stat() BEFORE reading". It was not.

    `read_file` loaded the whole file to hand back a `max_read_chars` window, so a
    multi-gigabyte file inside a root went into memory on every call -- exactly what the
    setting's own description promises cannot happen. `context.prefetch` honoured it; this
    consumer did not.
    """
    (workspace / "huge.py").write_text("y" * 5000, encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace, max_file_read_bytes=1000),
        call("read_file", path=str(workspace / "huge.py")), tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "5000 bytes" in result.content
    assert "1000-byte ceiling" in result.content


@posix_only
def test_read_file_still_pages_a_file_under_the_ceiling(workspace):
    """The other direction. A ceiling that refused everything would pass the test above."""
    (workspace / "fine.py").write_text("z\n" * 400, encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace, max_file_read_bytes=1000, max_read_chars=100),
        call("read_file", path=str(workspace / "fine.py")), tools.ALL_TOOL_NAMES)
    assert not result.is_error
    assert "of 400" in result.content and "start_line=" in result.content


@posix_only
def test_read_file_numbers_every_line_it_returns(workspace):
    """The whole point of the change. Without a number in the output the model can read a
    range but cannot cite it, which is how one agent file came to be told to quote instead
    and warned that it is never shown a line number."""
    (workspace / "n.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "n.py")),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error
    assert result.content == "1  alpha\n2  beta\n3  gamma"


@posix_only
def test_read_file_starts_where_it_is_asked_to(workspace):
    """The other half: being pointed at a range, rather than paging to it."""
    (workspace / "n.py").write_text("".join(f"line {i}\n" for i in range(1, 11)),
                                    encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "n.py"), start_line=8),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error
    assert result.content == " 8  line 8\n 9  line 9\n10  line 10"


@posix_only
def test_read_file_pages_on_whole_lines_and_reports_the_next_one(workspace):
    """`max_read_chars` still bounds the reply, but the window now ends on a line boundary.

    Half a line carrying a line number would be worse than no number at all: the number
    would be a claim about a line the caller was not actually given.
    """
    (workspace / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 21)),
                                      encoding="utf-8")
    c = cfg(workspace, max_read_chars=40)
    first = tools.execute_tool(
        c, call("read_file", path=str(workspace / "big.py")), tools.ALL_TOOL_NAMES)
    assert not first.is_error
    body, _, footer = first.content.partition("\n\n[truncated:")
    assert footer, "a file over the window must say so"
    assert "of 20" in footer
    for line in body.split("\n"):
        # Every returned line is complete: a number, two spaces, then the whole line.
        number, _, rest = line.strip().partition("  ")
        assert rest == f"line {number}"


@posix_only
def test_read_file_continues_from_the_line_it_reported(workspace):
    """The footer is only useful if following it works, and lands exactly where it said."""
    (workspace / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 21)),
                                      encoding="utf-8")
    c = cfg(workspace, max_read_chars=40)
    first = tools.execute_tool(
        c, call("read_file", path=str(workspace / "big.py")), tools.ALL_TOOL_NAMES)
    resume = int(first.content.split("start_line=")[1].split(" ")[0])
    tail = tools.execute_tool(
        c, call("read_file", path=str(workspace / "big.py"), start_line=resume),
        tools.ALL_TOOL_NAMES)
    assert tail.content.split("\n")[0].strip().startswith(f"{resume}  line {resume}")


@posix_only
def test_read_file_refuses_a_start_line_past_the_end(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py"), start_line=999),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "past the end" in result.content


@posix_only
def test_read_file_refuses_a_start_line_below_one(workspace):
    """Lines are counted from 1, so 0 is not "the beginning" -- and negative indices would
    silently read from the end, which is the reading nobody meant."""
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "a.py"), start_line=0),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "counted from 1" in result.content


@posix_only
def test_a_file_without_a_trailing_newline_is_not_one_line_longer(workspace):
    """`split("\n")` would invent an empty final line for every file that ends in a
    newline, and the count reported to the model would be wrong by one for most files."""
    (workspace / "no_nl.py").write_text("one\ntwo", encoding="utf-8")
    (workspace / "with_nl.py").write_text("one\ntwo\n", encoding="utf-8")
    c = cfg(workspace)
    without = tools.execute_tool(
        c, call("read_file", path=str(workspace / "no_nl.py")), tools.ALL_TOOL_NAMES)
    withit = tools.execute_tool(
        c, call("read_file", path=str(workspace / "with_nl.py")), tools.ALL_TOOL_NAMES)
    assert without.content == withit.content == "1  one\n2  two"


@posix_only
def test_crlf_lines_are_numbered_the_same_as_lf_ones(workspace):
    """The boundary crosses Windows and WSL, so a file written on one side is read on the
    other. A carriage return left on the end of each line would reach the model as content."""
    (workspace / "crlf.py").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = tools.execute_tool(
        cfg(workspace), call("read_file", path=str(workspace / "crlf.py")),
        tools.ALL_TOOL_NAMES)
    assert result.content == "1  one\n2  two\n3  three"


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


BWRAP_REASON = (
    "EXIT-CODE CAPTURE UNPROVEN BY THIS RUN -- this is not a pass. Capturing a real "
    "exit code needs a real bubblewrap, which exists in WSL and not on Windows. Run: "
    "wsl -d Ubuntu-24.04 -e bash -lc 'cd <repo> && ~/.venvs/delegate/bin/python -m "
    "pytest tests/test_tools.py'"
)


def needs_bwrap(fn):
    """Both markers, always, because either alone is not enough.

    `skipif` on a missing bubblewrap is what keeps this quiet on Windows. It is *not* what
    keeps it quiet in CI: the runner installs bubblewrap, so `which` finds one, and then
    every invocation fails anyway because the sandbox cannot bring up loopback in a new
    network namespace without CAP_NET_ADMIN. The `integration` marker is what CI excludes,
    and a test carrying only the skipif runs there and fails.

    Composed into one decorator rather than left as two, because that is exactly the pair
    that got separated once.
    """
    return pytest.mark.integration(
        pytest.mark.skipif(shutil.which("bwrap") is None, reason=BWRAP_REASON)(fn))



# --- edit_file ---------------------------------------------------------------------------
#
# The refusals are the tool, not the edge cases of it: a quotation that matches nothing or
# matches twice means the model is not editing what it thinks it is, and every one of these
# asserts the file is byte-identical afterwards. A test that only checked the message would
# pass against a tool that refused and wrote anyway.

MODULE = """def one():
    return 1


def two():
    return 1
"""


@posix_only
def test_edit_file_replaces_a_unique_occurrence(workspace):
    target = workspace / "mod.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="beta", new_string="BETA"),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    assert "Replaced 1 occurrence" in result.content


@posix_only
def test_edit_file_leaves_the_file_alone_when_the_quotation_is_stale(workspace):
    """Zero matches. The whole reason this addresses text rather than line numbers: a stale
    line number silently overwrites the wrong region, and a stale quotation cannot."""
    target = workspace / "mod.py"
    target.write_text(MODULE, encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="return 3", new_string="return 4"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "does not appear" in result.content
    assert target.read_text(encoding="utf-8") == MODULE


@posix_only
def test_edit_file_refuses_an_ambiguous_quotation_and_says_how_many(workspace):
    """Two matches, which is the case that would corrupt a file quietly if it replaced the
    first one -- the model would be told it succeeded and be looking at the wrong function."""
    target = workspace / "mod.py"
    target.write_text(MODULE, encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="return 1", new_string="return 2"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "appears 2 times" in result.content
    assert target.read_text(encoding="utf-8") == MODULE


@posix_only
def test_edit_file_deletes_when_new_string_is_empty(workspace):
    target = workspace / "mod.py"
    target.write_text("keep\ndrop\nkeep\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="drop\n", new_string=""),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    assert target.read_text(encoding="utf-8") == "keep\nkeep\n"


@posix_only
def test_edit_file_shrinking_a_file_truncates_what_is_left_over(workspace):
    """The descriptor is reused rather than reopened, so a missing truncate would leave the
    tail of the old contents behind -- a corruption the byte count would not reveal."""
    target = workspace / "mod.py"
    target.write_text("aaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="aaaaaaaaaaaaaaaaaaaa",
             new_string="a"),
        tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    assert target.read_bytes() == b"a\n"


@posix_only
def test_edit_file_refuses_an_empty_old_string(workspace):
    """It matches at every position and identifies nothing, so it is a caller error rather
    than an edit of zero characters."""
    target = workspace / "mod.py"
    target.write_text(MODULE, encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="", new_string="x"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "empty" in result.content
    assert target.read_text(encoding="utf-8") == MODULE


@posix_only
def test_edit_file_refuses_a_no_op(workspace):
    """A turn spent changing nothing should say so rather than report a success."""
    target = workspace / "mod.py"
    target.write_text(MODULE, encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="return 1", new_string="return 1"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "identical" in result.content


@posix_only
def test_edit_file_refuses_a_result_over_the_write_limit(workspace):
    """The same limit write_file uses, and nothing is written -- the refusal has to come
    before the seek, not after it."""
    target = workspace / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace, max_write_bytes=10),
        call("edit_file", path=str(target), old_string="1", new_string="1" * 50),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "over the 10-byte limit" in result.content
    assert target.read_text(encoding="utf-8") == "x = 1\n"


@posix_only
def test_edit_file_refuses_a_file_that_is_not_text(workspace):
    """decode_text, shared with read_file, so a binary file gets one answer from both."""
    target = workspace / "blob.py"
    target.write_bytes(b"\x00\x01binary")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(target), old_string="binary", new_string="text"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert target.read_bytes() == b"\x00\x01binary"


@posix_only
def test_edit_file_obeys_the_path_policy_like_every_other_file_tool(workspace, tmp_path):
    """It goes through `_one_path`, so this asserts the layers reach it rather than
    re-testing them: a path outside every root, and an unlisted extension."""
    outside = tmp_path.parent / "elsewhere.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(outside), old_string="x = 1", new_string="x = 2"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert outside.read_text(encoding="utf-8") == "x = 1\n"

    blocked = workspace / "bundle.wasm"
    blocked.write_text("x = 1\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(blocked), old_string="x = 1", new_string="x = 2"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert blocked.read_text(encoding="utf-8") == "x = 1\n"


@posix_only
def test_edit_file_refuses_a_missing_file_rather_than_creating_one(workspace):
    """`must_exist=True`, unlike write_file: an edit to a file that is not there is a
    mistake about which file, and creating it would hide that."""
    result = tools.execute_tool(
        cfg(workspace),
        call("edit_file", path=str(workspace / "nope.py"), old_string="a", new_string="b"),
        tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert not (workspace / "nope.py").exists()


def test_edit_file_is_not_offered_to_a_read_only_delegation():
    """Derived from `writes`, so this cannot pass by anyone remembering to update a list."""
    assert "edit_file" not in tools.READ_ONLY_TOOL_NAMES
    assert "edit_file" not in {s.name for s in tools.declared_tools(tools.READ_ONLY_TOOL_NAMES)}

# --- run_bash ---------------------------------------------------------------------------


def test_run_bash_refuses_when_bwrap_is_absent(workspace, monkeypatch):
    """ADR-0010: no bubblewrap means no shell, not an unconfined shell."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="echo hi"), tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "ADR-0010" in result.content


def test_the_refusal_names_confinement_as_the_reason(workspace, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="ls"), tools.ALL_TOOL_NAMES)
    assert "confined" in result.content.lower()
    assert "unconfined" in result.content.lower()


def test_a_refusal_reports_no_exit_code_but_still_counts_as_an_attempt(workspace, monkeypatch):
    """`ran` is what separates "nothing ran" from "ran and said nothing".

    Without it a refusal and a clean `exit 0` are both `exit_code`-shaped, and the ledger
    would either miscount attempts or invent an exit code no process produced.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="ls"), tools.ALL_TOOL_NAMES)
    assert result.bash is not None
    assert result.bash.ran is False
    assert result.bash.exit_code is None
    assert result.bash.timed_out is False


def test_the_route_is_open_and_nothing_narrows_it_any_more(workspace, monkeypatch):
    """The end of a chain of tests, each named after the guard that held the route shut.

    First a config setting turned the sandbox off; that setting was deleted. Then the
    handler refused unconditionally; it runs commands now. Then the withholding; it is empty
    as of M5. Naming each test after the guard rather than after the tool is what made the
    replacement obvious every time, instead of leaving an assertion that passed for a reason
    that had stopped being true.

    A fifth guard now exists and is deliberately stubbed out here rather than tested here:
    a host without bubblewrap does not get the tool offered. That is
    `test_run_bash_is_not_declared_where_bubblewrap_is_absent`; this test is about the
    narrowing that happens once the host can run it at all.
    """
    c = cfg(workspace)
    monkeypatch.setattr(sandbox, "available", lambda _cfg: True)
    assert "run_bash" in tools.available_tool_names(c)
    assert "run_bash" in tools.resolve_allowed(None, c)
    assert "run_bash" in tools.resolve_allowed(["run_bash", "read_file"], c)
    assert [s for s in tools.declared_tools(tools.resolve_allowed(None, c))
            if s.name == "run_bash"]


def test_run_bash_is_not_declared_where_bubblewrap_is_absent(workspace, monkeypatch):
    """The declaration asks the host, rather than remembering an import-time constant.

    M5 emptied `WITHHELD_TOOL_NAMES` and left `available_tool_names` taking no `Config`, so
    on a machine with no `bwrap` the tool was offered and then refused every call it was
    asked to make. A turn costs a round trip to learn something the server could have said
    for free (JOURNAL 2026-08-29).

    Proven by making the condition true rather than by asserting the code reads well: point
    `bwrap_bin` at a name that does not resolve, which is exactly what `sandbox.available`
    consults, and the tool leaves every set.
    """
    c = cfg(workspace, bwrap_bin="definitely-not-on-this-path-bwrap")
    assert not sandbox.available(c)
    assert "run_bash" not in tools.available_tool_names(c)
    assert "run_bash" not in tools.resolve_allowed(None, c)
    # And a caller naming it explicitly still cannot widen the set back, which is the
    # property `resolve_allowed` exists for.
    assert "run_bash" not in tools.resolve_allowed(["run_bash"], c)
    assert "run_bash" not in {s.name for s in tools.declared_tools(tools.resolve_allowed(None, c))}
    # The other two are untouched: this narrows one tool for one reason, not the set.
    assert tools.available_tool_names(c) == frozenset(
        {"read_file", "search_files", "read_git", "write_file", "edit_file"})


def test_the_absent_sandbox_does_not_weaken_the_execution_site(workspace, monkeypatch):
    """The trap CLAUDE.md names: narrowing the declared set is not enforcement.

    `execute_tool` takes its allowed set as a parameter and does not consult
    `available_tool_names`, so a model calling `run_bash` on a bwrap-less host must still be
    refused *by the executor* -- and for its own reason, not because the declaration
    happened to drop it. Asserted here so that a future change to one site cannot silently
    make the other the only one left.
    """
    c = cfg(workspace, bwrap_bin="definitely-not-on-this-path-bwrap")
    result = tools.execute_tool(c, call("run_bash", command="ls"), frozenset({"read_file"}))
    assert result.is_error


def test_the_policy_reaches_the_sandbox_request(workspace, monkeypatch):
    """`run_bash` was hardcoded to no workdir, no network and toolchain binds only.

    Every one of those is now the delegation's to decide, and the wiring is what this
    asserts: the executor's policy argument arrives at `SandboxRequest` unchanged. Captured
    at the boundary rather than run, because what is under test is the plumbing, and a real
    bwrap would prove the plumbing only incidentally.
    """
    seen: list[sandbox.SandboxRequest] = []

    def capture(cfg_, req):
        seen.append(req)
        return sandbox.SandboxResult(stdout="", stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr(sandbox, "run", capture)
    policy = tools.BashPolicy(
        workdir=str(workspace), network=True, extra_binds=("/opt/toolchain",))
    tools.execute_tool(
        cfg(workspace), call("run_bash", command="ls"), frozenset({"run_bash"}), policy)

    (req,) = seen
    assert req.workdir == str(workspace)
    assert req.network is True
    assert "/opt/toolchain" in req.extra_binds


def test_a_delegation_that_names_no_policy_reaches_nothing_of_yours(workspace, monkeypatch):
    """The default is the pre-M6 behaviour exactly, and it is the safe direction.

    A caller that says nothing must not silently inherit a workspace: `workdir=None` means
    the command runs in the sandbox HOME, with no network and only the binds the server
    probed for itself.
    """
    seen: list[sandbox.SandboxRequest] = []
    monkeypatch.setattr(sandbox, "run", lambda c, r: (
        seen.append(r),
        sandbox.SandboxResult(stdout="", stderr="", exit_code=0, timed_out=False))[1])
    monkeypatch.setattr(sandbox, "probe_toolchain_binds", lambda c: ())

    tools.execute_tool(cfg(workspace), call("run_bash", command="ls"), frozenset({"run_bash"}))
    (req,) = seen
    assert req.workdir is None
    assert req.network is False
    assert req.extra_binds == ()


def test_execute_tool_still_checks_its_own_allowed_set(workspace):
    """Site two. A model can name a tool it was never offered, so the executor checks too --
    and it never consulted the withholding, so emptying that set did not weaken this."""
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="ls"), frozenset({"read_file"}))
    assert result.is_error
    assert "not available" in result.content


@needs_bwrap
def test_a_real_exit_code_is_captured_not_taken_from_the_model(workspace):
    """The ground truth ADR-0007 rests on, through the tool layer rather than sandbox.run.

    Reachable without a mock even while run_bash is withheld: `execute_tool` takes `allowed`
    as a parameter and never consults WITHHELD_TOOL_NAMES, which is exactly why the
    withholding is not by itself a control (see the test above).
    """
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="exit 3"), tools.ALL_TOOL_NAMES)
    assert result.bash is not None
    assert result.bash.exit_code == 3
    assert result.bash.ran is True
    assert result.is_error is True


@needs_bwrap
def test_a_clean_command_reports_zero_and_is_not_an_error(workspace):
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="echo hello"), tools.ALL_TOOL_NAMES)
    assert result.bash.exit_code == 0
    assert result.is_error is False
    assert "hello" in result.content


@needs_bwrap
def test_stdout_and_stderr_are_labelled_separately(workspace):
    """Merged, a model cannot tell which stream it is reading, and they mean different
    things -- a command that printed nothing to stdout is a different fact from one that
    printed a warning."""
    result = tools.execute_tool(
        cfg(workspace),
        call("run_bash", command="echo out; echo err >&2"),
        tools.ALL_TOOL_NAMES)
    assert "stdout:" in result.content
    assert "stderr:" in result.content


@needs_bwrap
def test_long_output_is_cut_to_the_tail_with_the_true_length_stated(workspace):
    """The tail, not the head: a build says what went wrong on its last line."""
    result = tools.execute_tool(
        cfg(workspace, max_bash_output_chars=200),
        call("run_bash", command="seq 1 5000"),
        tools.ALL_TOOL_NAMES)
    assert "truncated" in result.content
    assert "5000" in result.content
    assert result.bash.exit_code == 0


@needs_bwrap
def test_a_timeout_says_so_and_reports_no_exit_code(workspace):
    """None rather than a number, and said in words: a model summarising its own run must
    not be able to read "no exit code" as success."""
    result = tools.execute_tool(
        cfg(workspace, run_bash_timeout=2),
        call("run_bash", command="sleep 30"),
        tools.ALL_TOOL_NAMES)
    assert result.bash.timed_out is True
    assert result.bash.exit_code is None
    assert result.bash.ran is True
    assert "timed out" in result.content.lower()
    assert result.is_error is True


# --- the write_file steer (ADR-0024) ------------------------------------------------------


@pytest.mark.parametrize("command", [
    "sed -i s/a/b/ config.py",
    "perl -pi -e s/x/y/ notes.md",
    "awk -i inplace 1 f.txt",
    "cat > out.txt << EOF",
    "echo hi >> server.log",
    "tee settings.json",
    "patch < fix.diff",
])
def test_shell_text_patching_is_recognised(command):
    assert tools._rewrites_text(command) is True


@pytest.mark.parametrize("command", [
    "ls -la",
    "grep needle haystack.txt | head",
    "pytest -q",
    "python -c 'print(1)' > /dev/null",
    "curl -s -o /dev/null http://example",
    "make 2>&1",
])
def test_reading_and_discarding_are_not_patching(command):
    """The half that makes the steer worth having. A note on every command is a note the
    model learns to skip, which is the same as no note."""
    assert tools._rewrites_text(command) is False


def test_the_steer_is_appended_when_write_file_is_available(workspace, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="sed -i s/a/b/ f.py"),
        {"run_bash", "write_file"})
    assert "write_file is available" in result.content


def test_the_steer_is_withheld_when_write_file_is_not_in_the_resolved_set(workspace, monkeypatch):
    """Gated on the set the executor enforces, not the declared list. Steering toward a tool
    this same function would refuse is worse than staying quiet."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="sed -i s/a/b/ f.py"), {"run_bash"})
    assert "write_file is available" not in result.content


def test_the_steer_is_advisory_and_never_changes_the_outcome(workspace, monkeypatch):
    """Advisory means the result stands: same error flag, same measured outcome, one extra
    paragraph. A steer that altered either would be a block wearing a note's clothing."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    steered = tools.execute_tool(
        cfg(workspace), call("run_bash", command="sed -i s/a/b/ f.py"),
        {"run_bash", "write_file"})
    plain = tools.execute_tool(
        cfg(workspace), call("run_bash", command="sed -i s/a/b/ f.py"), {"run_bash"})
    assert steered.is_error == plain.is_error
    assert steered.bash == plain.bash
    assert steered.content.startswith(plain.content)


def test_an_ordinary_command_gets_no_steer_even_with_write_file_available(workspace, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = tools.execute_tool(
        cfg(workspace), call("run_bash", command="ls -la"), {"run_bash", "write_file"})
    assert "write_file is available" not in result.content


# --- search_files -----------------------------------------------------------------------
#
# The tool exists because grep-read-grep was the one research shape a delegation could not
# do: `files[]` needs the caller to already know where to look. What it must never become
# is a way to read what `read_file` refuses, so most of what follows is the path policy
# asserted through the new entry point rather than the old one.


@pytest.fixture
def haystack(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return alpha()\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "helper.py").write_text("# alpha lives elsewhere\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("alpha is documented here\n", encoding="utf-8")
    return tmp_path


def _search(root: Path, **args) -> str:
    result = tools.execute_tool(
        cfg(root), call("search_files", **args), tools.ALL_TOOL_NAMES)
    assert not result.is_error, result.content
    return result.content


@posix_only
def test_a_match_is_reported_with_its_file_and_line_number(haystack):
    out = _search(haystack, pattern=r"def alpha")
    assert "core.py line 1: def alpha():" in out


@posix_only
def test_the_citation_never_joins_the_name_and_number_with_a_colon(haystack):
    """A name joined to a number by a colon reads as a host and a port once the number
    reaches four digits, and this project refuses that shape wherever text can reach a
    commit message or a pull request body. The tool's own output is the place to get it
    right rather than something to remember later.

    Written as a mechanism and not as a specimen, deliberately: the first draft of this
    docstring spelled the forbidden shape out to explain it, and the gate blocked the
    commit. CLAUDE.md says this rule cannot show you the shape it forbids, which is the
    point -- and the check proved it holds on a file arguing for it."""
    out = _search(haystack, pattern=r"alpha")
    assert " line " in out
    assert "core.py:1" not in out


@posix_only
def test_it_searches_the_whole_workspace_when_no_path_is_given(haystack):
    """The point of the tool. With `path` required it would be a second `read_file`."""
    out = _search(haystack, pattern=r"alpha")
    assert "core.py" in out
    assert "helper.py" in out
    assert "notes.md" in out


@posix_only
def test_a_path_narrows_the_search(haystack):
    out = _search(haystack, pattern=r"alpha", path=str(haystack / "pkg"))
    assert "core.py" in out
    assert "notes.md" not in out


@posix_only
def test_a_glob_narrows_which_files_are_opened(haystack):
    out = _search(haystack, pattern=r"alpha", glob="*.md")
    assert "notes.md" in out
    assert "core.py" not in out


@posix_only
def test_no_match_says_so_rather_than_returning_nothing(haystack):
    """An empty string reads like a broken tool. It also has to avoid implying proof of
    absence, since the policy may simply not allow reading where the answer is."""
    out = _search(haystack, pattern=r"gamma_never_appears")
    assert "No line matched" in out
    assert "policy" in out


@posix_only
def test_an_invalid_regex_is_a_refusal_the_model_can_correct(haystack):
    result = tools.execute_tool(
        cfg(haystack), call("search_files", pattern="alpha("), tools.ALL_TOOL_NAMES)
    assert result.is_error
    assert "regular expression" in result.content


@posix_only
def test_max_results_bounds_the_reply_and_the_reply_says_it_stopped(haystack):
    out = _search(haystack, pattern=r"alpha", max_results=1)
    assert len([ln for ln in out.splitlines() if " line " in ln]) == 1
    assert "Stopped early" in out


@posix_only
def test_a_path_outside_the_workspace_is_refused(haystack, tmp_path_factory):
    """Layer 2, through the new entry point. A search that could be pointed anywhere would
    be a way around the policy that governs every read."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("alpha\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(haystack), call("search_files", pattern="alpha", path=str(outside)),
        tools.ALL_TOOL_NAMES)
    assert result.is_error


@posix_only
def test_an_unlisted_extension_is_never_opened(haystack):
    """Layer 3's neighbour: the extension allowlist. Enumeration must not widen what a
    delegation can see -- a candidate `read_file` would refuse is not a result."""
    (haystack / "pkg" / "private.pem").write_text("alpha-key-material\n", encoding="utf-8")
    out = _search(haystack, pattern=r"alpha")
    assert "private.pem" not in out
    assert "key-material" not in out


@posix_only
def test_a_denylisted_file_is_not_searched(haystack):
    """The secret denylist, which is the same list `read_file` and the sandbox read."""
    (haystack / "pkg" / ".env").write_text("TOKEN=alpha\n", encoding="utf-8")
    out = _search(haystack, pattern=r"alpha")
    assert ".env" not in out
    assert "TOKEN" not in out


@posix_only
def test_the_two_dispositions_differ_on_the_same_policy(haystack, tmp_path_factory):
    """Why `resolve_permitted` exists beside `resolve_all` rather than as a flag on it.

    All-or-nothing is right for paths a caller *named*: a delegation that asked for six
    files and silently got five reads exactly like one that got six. It is wrong for
    candidates a walk enumerated, where one refused path would fail the whole search. Same
    four layers either way -- asserted here in both directions over one input, because
    that is the only way to show the policy is shared and only the disposition differs.
    """
    outside = tmp_path_factory.mktemp("outside")
    (outside / "x.py").write_text("alpha\n", encoding="utf-8")
    inside = str(haystack / "notes.md")
    both = [inside, str(outside / "x.py")]
    conf = cfg(haystack)

    with pytest.raises(tools.PathRefused):
        tools.resolve_all(conf, both)

    kept = tools.resolve_permitted(conf, both)
    assert [k.posix for k in kept] == [os.path.realpath(inside)]


@posix_only
def test_the_scan_cap_is_reported_rather_than_passing_as_exhaustive(haystack):
    """A truncated search that reads like an exhaustive one is the failure worth avoiding:
    the model concludes a symbol does not exist, and says so confidently."""
    for i in range(6):
        (haystack / "pkg" / f"m{i}.py").write_text("filler\n", encoding="utf-8")
    result = tools.execute_tool(
        cfg(haystack, search_max_files_scanned=2),
        call("search_files", pattern="zzz_absent"), tools.ALL_TOOL_NAMES)
    assert not result.is_error
    assert "not exhaustive" in result.content


# --- the read-only guarantee ------------------------------------------------------------


def test_the_read_only_set_is_derived_from_the_registry_not_listed():
    """ADR-0048's promise is that nothing a read-only delegation can do will write.

    Asserted against the registry rather than against a written-out list, because a list is
    the shape this project keeps calling a check that cannot fail: add a writing tool,
    forget to edit the list, and the annotation is still advertised while being false.
    """
    assert tools.READ_ONLY_TOOL_NAMES == frozenset(
        name for name, tool in tools.REGISTRY.items() if not tool.writes
    )
    assert tools.READ_ONLY_TOOL_NAMES == {"read_file", "search_files", "read_git"}
    for name in tools.READ_ONLY_TOOL_NAMES:
        assert not tools.REGISTRY[name].writes


def test_every_writing_tool_declares_that_it_writes():
    """The other direction, and the one that matters: a tool that writes and forgot to say
    so would be silently offered to a delegation declared read-only."""
    assert {n for n, t in tools.REGISTRY.items() if t.writes} == {
        "write_file",
        "edit_file",
        "run_bash",
    }


@posix_only
def test_a_write_is_refused_at_execution_for_a_read_only_toolset(workspace):
    """Site two, not site one. Filtering the declared list is advisory -- a model can call
    a tool it was never offered, and some do -- so the check that matters is the one at
    execution, and this asserts that one.
    """
    for name, args in (
        ("write_file", {"path": str(workspace / "new.py"), "content": "x = 2\n"}),
        (
            "edit_file",
            {
                "path": str(workspace / "mod.py"),
                "old_string": "x = 1",
                "new_string": "x = 2",
            },
        ),
        ("run_bash", {"command": "echo hi"}),
    ):
        result = tools.execute_tool(
            cfg(workspace), call(name, **args), tools.READ_ONLY_TOOL_NAMES)
        assert result.is_error, f"{name} was executed for a read-only delegation"
    assert not (workspace / "new.py").exists(), "the file was written despite the refusal"


def test_the_read_only_set_is_what_is_declared_to_the_model(workspace):
    """Site one, for completeness: offered and executable agree for this set."""
    names = sorted(s.name for s in tools.declared_tools(tools.READ_ONLY_TOOL_NAMES))
    assert names == ["read_file", "read_git", "search_files"]


# --- read_git ---------------------------------------------------------------------------
#
# The refusals are the point. `.git` is on the secret denylist so the sandbox cannot reach
# history at all, and this tool is the way in -- which makes its allowlist the whole of the
# control. Every case below asserts a refusal happened *and* names why, because a test that
# only checked "an error came back" would pass against a tool that ran git and then
# complained about the exit code.

git_only = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git on PATH"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with several commits, so there is history to read *and* to
    truncate. One commit would not exercise the cap: the first line is always kept,
    the way `read_file` never returns an empty window."""
    import subprocess

    r = tmp_path / "repo"
    r.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def run(*argv: str) -> None:
        subprocess.run(argv, cwd=r, env=env, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    for n in range(1, 6):
        (r / "kept.py").write_text(f"x = {n}\n", encoding="utf-8")
        run("git", "add", "kept.py")
        run("git", "commit", "-q", "-m", f"feat: change number {n}")
    return r


def git_call(root: Path, **args):
    return tools.execute_tool(cfg(root), call("read_git", **args), tools.ALL_TOOL_NAMES)


@git_only
@posix_only
def test_read_git_returns_real_history(repo):
    result = git_call(repo.parent, repo=str(repo), command="log", args=["--oneline"])
    assert not result.is_error, result.content
    assert "change number 5" in result.content


@git_only
@posix_only
def test_read_git_accepts_a_path_argument(repo):
    result = git_call(
        repo.parent, repo=str(repo), command="log",
        args=["--oneline"], paths=["kept.py"],
    )
    assert not result.is_error, result.content
    assert "change number 5" in result.content


@git_only
@posix_only
def test_shortlog_is_given_a_revision_rather_than_reading_stdin(repo):
    """`git shortlog` takes its commits from stdin when stdin is not a terminal, so with
    stdin closed it succeeds and prints nothing. Found by running the tool, not by reading
    it: an empty answer here reads exactly like "no commits by anyone"."""
    result = git_call(repo.parent, repo=str(repo), command="shortlog", args=["-s"])
    assert not result.is_error, result.content
    assert "Test" in result.content, "shortlog returned nothing, so it read stdin"


@pytest.mark.parametrize("command", ["commit", "push", "fetch", "gc", "config", "clone"])
def test_a_subcommand_that_is_not_read_only_is_refused(workspace, command):
    result = git_call(workspace, repo=str(workspace), command=command)
    assert result.is_error
    assert "not a git subcommand this tool runs" in result.content


@pytest.mark.parametrize(
    ("command", "args"),
    [
        # Each of these turns a read into a write or into running a program.
        ("log", ["-c", "core.pager=sh -c id"]),
        ("log", ["--output=/tmp/pwned"]),
        ("show", ["--output=/tmp/pwned"]),
        ("show", ["-o", "/tmp/pwned"]),
        ("diff", ["--ext-diff"]),
        ("log", ["--exec-path=/tmp"]),
        ("log", ["--upload-pack=sh"]),
        ("blame", ["--exec-path=/tmp"]),
    ],
)
def test_a_flag_that_could_write_or_execute_is_refused(workspace, command, args):
    result = git_call(workspace, repo=str(workspace), command=command, args=args)
    assert result.is_error
    assert "is not an accepted flag" in result.content


@pytest.mark.parametrize("given", ["/etc/passwd", "../../../etc/passwd", "a/../../b"])
def test_a_path_that_leaves_the_repository_is_refused(workspace, given):
    result = git_call(workspace, repo=str(workspace), command="log", paths=[given])
    assert result.is_error
    assert "repository" in result.content


def test_the_separator_cannot_be_passed_by_hand(workspace):
    """`--` is added around `paths` by the tool. Accepting it in `args` would let a path
    be smuggled in through the channel that is not path-checked."""
    result = git_call(workspace, repo=str(workspace), command="log", args=["--", "x"])
    assert result.is_error
    assert "Do not pass '--' yourself" in result.content


@posix_only
def test_a_repository_outside_the_workspace_is_refused(workspace, tmp_path):
    outside = tmp_path.parent / "outside-the-roots"
    outside.mkdir(exist_ok=True)
    result = git_call(workspace, repo=str(outside), command="log")
    assert result.is_error
    assert "workspace roots" in result.content


@git_only
@posix_only
def test_a_directory_inside_the_repository_resolves_to_its_root(repo):
    """`-C` makes git discover a repository by walking up, so what it resolves to is
    checked as well as what the caller wrote -- see the handler's docstring for why that
    is not paranoia."""
    inner = repo / "pkg"
    inner.mkdir()
    result = git_call(
        repo.parent, repo=str(inner), command="rev-parse", args=["--show-toplevel"]
    )
    assert not result.is_error, result.content
    assert result.content.strip().endswith("repo")


@git_only
@posix_only
def test_long_output_is_truncated_on_a_line_boundary_and_says_so(repo):
    """A cut-off history that reads like a complete one is how a model concludes a commit
    does not exist."""
    result = tools.execute_tool(
        cfg(repo.parent, max_read_chars=40),
        call("read_git", repo=str(repo), command="log", args=["--format=%H %s"]),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, result.content
    assert "[truncated:" in result.content


def test_read_git_is_declared_and_executable_from_one_registry_entry():
    """Both enforcement sites read `REGISTRY`, so a tool cannot be offered without being
    runnable or the other way round. Asserted rather than assumed, because the asymmetry
    is the trap CLAUDE.md records for `allowed_tools`."""
    assert "read_git" in tools.REGISTRY
    assert "read_git" in {s.name for s in tools.declared_tools({"read_git"})}
    assert "read_git" not in {s.name for s in tools.declared_tools({"read_file"})}
    # Withheld from the declared list, it must still be refused by the executor.
    result = tools.execute_tool(
        cfg(Path.cwd()), call("read_git", repo=".", command="log"), {"read_file"}
    )
    assert result.is_error
    assert "not available in this delegation" in result.content


def test_a_host_without_git_is_not_offered_the_tool(workspace, monkeypatch):
    """The reasoning `run_bash` established: declaring a tool the host cannot run spends a
    whole turn teaching the model what this function already knows.

    Only `git` is hidden, not every executable. `shutil` is one module object shared with
    `sandbox`, so blanking `which` outright would take `run_bash` away too and this would
    pass for a reason it does not claim -- the broader mechanism would still satisfy the
    narrower assertion, which is how a test stops testing what its name says.
    """
    real = tools.shutil.which
    monkeypatch.setattr(
        tools.shutil, "which", lambda name, *a, **k: None if name == "git" else real(name, *a, **k)
    )
    names = tools.available_tool_names(cfg(workspace))
    assert "read_git" not in names
    assert "read_file" in names, "the whole set went away, so this proves nothing"


@git_only
@posix_only
def test_the_count_shorthand_is_accepted(repo):
    """`git log -1` is what a model reaches for first. A live delegation spent a turn
    being told to write `-n 1` instead, which is a synonym it already knew."""
    result = git_call(repo.parent, repo=str(repo), command="log", args=["-1", "--format=%s"])
    assert not result.is_error, result.content
    assert result.content.strip() == "feat: change number 5"


@git_only
@posix_only
def test_commits_can_be_counted(repo):
    """`rev-list --count` is the canonical way, and `log` alone cannot do it -- there is no
    pipe to `wc` here. The same delegation reached for this and was refused."""
    result = git_call(
        repo.parent, repo=str(repo), command="rev-list",
        args=["--count", "HEAD"], paths=["kept.py"],
    )
    assert not result.is_error, result.content
    assert result.content.strip() == "5"


def test_the_shorthand_is_not_a_hole_for_other_flags(workspace):
    """A digit is a count; anything else after the dash is still checked. And `shortlog -n`
    means --numbered, so the shorthand deliberately does not apply there."""
    for args in (["-1x"], ["--output=/tmp/x"], ["-o", "/tmp/x"]):
        result = git_call(workspace, repo=str(workspace), command="log", args=args)
        assert result.is_error, f"{args} was accepted"
    assert "-5" not in tools.GIT_SUBCOMMANDS["shortlog"]
    assert "shortlog" not in tools.GIT_COUNT_SHORTHAND
