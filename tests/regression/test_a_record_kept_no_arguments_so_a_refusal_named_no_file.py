"""What the per-call record keeps of the arguments, and what it refuses to keep.

The record exists because a delegation reporting `tool_errors: 1` across twelve
`read_git` calls could not say which call failed. Naming the tool was never the missing
half -- the arguments were. So these check the boundary ADR-0060 draws: identifiers are
recorded, file bodies are reduced to a length and a digest, and nothing is trimmed
without saying it was trimmed.

Every assertion here has a negative twin, because CLAUDE.md's rule is that a check which
cannot fail is worse than no check, and this repository has found six of those.
"""

from __future__ import annotations

from claude_delegate_local.backends.base import BashOutcome, ToolResultBlock, ToolUseBlock
from claude_delegate_local.loop import (
    TOOL_ARG_VALUE_CAP,
    record_arguments,
    tool_call_record,
)


def call(name: str = "read_file", **arguments: object) -> ToolUseBlock:
    return ToolUseBlock(id="t1", name=name, input=dict(arguments))


def result(content: str = "ok", *, is_error: bool = False, bash: BashOutcome | None = None):
    return ToolResultBlock(tool_use_id="t1", content=content, is_error=is_error, bash=bash)


def test_the_arguments_are_recorded_so_a_refusal_names_its_file():
    """The gap the record was opened to close: twelve `read_git` calls, one error, and
    no way to tell which."""
    record = tool_call_record(call("read_git", repo=".", command="log -1"), "error",
                              result("refused", is_error=True))

    assert dict(record.arguments) == {"command": "log -1", "repo": "."}


def test_arguments_are_sorted_so_two_records_of_one_call_compare_equal():
    """Backends serialise an argument object in whatever order they like. A record whose
    equality depended on that order would make every test on it flaky somewhere else."""
    one = record_arguments(call("read_git", repo=".", command="log -1"))
    other = record_arguments(call("read_git", command="log -1", repo="."))

    assert one == other


def test_a_long_argument_is_cut_and_says_so():
    """A silently truncated argument reads as a complete one, so a reader would draw a
    conclusion about a path the model never sent."""
    long_path = "/" + "a" * (TOOL_ARG_VALUE_CAP + 50)
    recorded = dict(record_arguments(call(path=long_path)))["path"]

    assert len(recorded) < len(long_path)
    assert recorded.startswith(long_path[:TOOL_ARG_VALUE_CAP])
    assert "[+51 chars]" in recorded, recorded


def test_a_short_argument_is_untouched():
    """The negative twin of the cap. A check that elides everything would pass the test
    above and destroy the record."""
    recorded = dict(record_arguments(call(path="/a.py")))["path"]

    assert recorded == "/a.py"
    assert "chars]" not in recorded


def test_a_written_body_is_a_length_and_a_digest_not_its_bytes():
    """ADR-0039 kept file bodies out of the record; `write_file` takes one as an
    argument, which is the door this closes."""
    body = "SECRET-LINE\n" * 40
    recorded = dict(record_arguments(call("write_file", path="/a.py", content=body)))

    assert "SECRET-LINE" not in recorded["content"], recorded["content"]
    assert recorded["content"].startswith(f"<{len(body)} chars, sha256:")
    assert recorded["path"] == "/a.py", "the identifier is still recorded"


def test_two_different_bodies_are_distinguishable_and_one_body_is_stable():
    """Why a digest rather than nothing at all. The only question a record is asked
    about a body is which of two writes actually ran."""
    first = dict(record_arguments(call("write_file", path="/a.py", content="one")))
    again = dict(record_arguments(call("write_file", path="/a.py", content="one")))
    other = dict(record_arguments(call("write_file", path="/a.py", content="two")))

    assert first["content"] == again["content"]
    assert first["content"] != other["content"]


def test_an_edit_records_neither_side_of_the_replacement_verbatim():
    """`old_string` and `new_string` are bodies too. Recording either would put the file
    in the record in two halves rather than one."""
    recorded = dict(record_arguments(
        call("edit_file", path="/a.py", old_string="BEFORE-TEXT", new_string="AFTER-TEXT")
    ))

    assert "BEFORE-TEXT" not in recorded["old_string"]
    assert "AFTER-TEXT" not in recorded["new_string"]
    assert recorded["old_string"] != recorded["new_string"]


def test_a_successful_call_records_accounting_and_no_content():
    """On success the record carries how much came back, never what came back -- a
    successful `read_file`'s result *is* the file."""
    record = tool_call_record(call(path="/a.py"), "ran", result("line one\nline two\n"))

    assert record.message == ""
    assert (record.result_bytes, record.result_lines) == (18, 2)


def test_an_empty_result_is_no_lines_rather_than_one():
    """`count("\\n") + 1` alone reports a line that is not there, and "one line" is what
    an operator would read as a `search_files` that found a match."""
    record = tool_call_record(call("search_files", pattern="zzz"), "ran", result(""))

    assert record.result_lines == 0


def test_an_exit_code_is_recorded_only_when_a_process_exited():
    """`None` covers both "no shell command here" and "killed before it could exit",
    and neither is 0 -- a real exit code that must not collide with either."""
    ran = tool_call_record(call("run_bash", command="false"), "error",
                           result("failed", is_error=True,
                                  bash=BashOutcome(exit_code=1, ran=True)))
    killed = tool_call_record(call("run_bash", command="sleep 99"), "error",
                              result("timed out", is_error=True,
                                     bash=BashOutcome(exit_code=None, timed_out=True, ran=True)))
    no_shell = tool_call_record(call(path="/a.py"), "ran", result("x"))

    assert ran.exit_code == 1
    assert killed.exit_code is None
    assert no_shell.exit_code is None


def test_absent_fields_are_absent_from_the_json_rather_than_null():
    """A key present and empty reads as a measured empty, so a call that ran no shell
    command must not report `exit_code: null` beside one that was killed."""
    row = tool_call_record(call(path="/a.py"), "ran", result("x")).as_json()

    assert "exit_code" not in row
    assert "message" not in row
    assert row["arguments"] == {"path": "/a.py"}
