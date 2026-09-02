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
    assert tools.available_tool_names(c) == frozenset({"read_file", "write_file"})


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
