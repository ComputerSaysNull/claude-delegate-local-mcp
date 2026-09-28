"""A truncated result's `result_lines` counted the note saying it was truncated.

`read_file` and `read_git` end a cut result with a blank line and a `[truncated: ...]`
line; `run_bash` opens one with the note. The record counted every line of the content, so
a 657-line read showed as 659 in the viewer, and two consecutive reads looked as if they
overlapped by two lines. The note is the server's, not the file's, so the count leaves it
out and `result_bytes` still measures what was sent.
"""

from __future__ import annotations

from claude_delegate_local.backends.base import BashOutcome, ToolResultBlock, ToolUseBlock
from claude_delegate_local.loop import _TRUNCATION_MARKER, tool_call_record

BODY = "\n".join(f"{n}\tline {n}" for n in range(1, 658))


def _record(name: str, content: str, *, bash: BashOutcome | None = None):
    call = ToolUseBlock(id="t1", name=name, input={"path": "/a.py"})
    return tool_call_record(call, "ran", ToolResultBlock(
        tool_use_id="t1", content=content, bash=bash))


def test_a_truncated_read_counts_its_body_only():
    content = (BODY + f"\n\n{_TRUNCATION_MARKER} lines 1 to 657 of 900. Call read_file "
               "again with start_line=658 for the rest.]")
    record = _record("read_file", content)
    assert record.result_lines == 657
    assert record.result_bytes == len(content)


def test_a_truncated_history_counts_its_body_only():
    content = BODY + f"\n\n{_TRUNCATION_MARKER} 657 of 900 lines. Narrow it.]"
    assert _record("read_git", content).result_lines == 657


SHELL_NOTE = f"{_TRUNCATION_MARKER} 90000 characters of output, showing the last 20000.]"
BASH = BashOutcome(exit_code=0, ran=True, masked_failure=False, stages=())


def test_a_truncated_shell_output_counts_its_body_only():
    """`_run_bash` sends `exit N`, a blank line, then the capped output, note first.

    So the count is the two header lines plus the body -- the note is the only thing left out.
    """
    content = f"exit 0\n\n{SHELL_NOTE}\n{BODY}"
    assert _record("run_bash", content, bash=BASH).result_lines == 2 + 657


def test_a_timed_out_shell_output_counts_its_body_only():
    header = "Timed out after 600s and was killed. Output up to that point:"
    content = f"{header}\n\n{SHELL_NOTE}\n{BODY}"
    assert _record("run_bash", content, bash=BASH).result_lines == 2 + 657


def test_an_untruncated_read_is_counted_whole():
    """Control: nothing to strip, so the count is every line, as before."""
    assert _record("read_file", BODY).result_lines == 657


def test_a_file_quoting_the_note_is_not_mistaken_for_one():
    """Control: the note is only recognised where the tool puts it, as the last line.

    A file whose own text carries the marker mid-way keeps every line counted.
    """
    content = BODY + f"\n\n{_TRUNCATION_MARKER} quoted in a document]\nand one more line"
    assert _record("read_file", content).result_lines == 660
