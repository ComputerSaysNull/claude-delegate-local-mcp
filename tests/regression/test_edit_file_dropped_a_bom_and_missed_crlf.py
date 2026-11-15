"""`edit_file` dropped a file's UTF-8 BOM and missed every CRLF file's multi-line quote.

Two bugs with one cause: the tool matched and wrote the text `decode_text` returned, which
is neither the bytes on disk nor the text `read_file` shows. A BOM is stripped on the way
in and never put back, so a one-line edit silently rewrote the whole file's first bytes. A
CRLF file keeps its `\\r` in that text, while `read_file` prints lines with `\\n`, so a
multi-line `old_string` copied from it matched nothing and the edit was refused as "does
not appear in the file" -- the model's quotation was right and the tool disagreed.

The fix is in `edit_file` only, because `read_file` showing LF is the behaviour the model
depends on: the file's own form is remembered and matched against the LF form, and written
back as it came. A file with mixed endings is left alone rather than guessed at, since
converting it would rewrite lines the model never touched.
"""

from pathlib import Path


from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    # Layer 3 reads `security/secret_globs.txt`, which a nested sandbox replaces with an
    # unreadable placeholder and treats as fatal. Point it at a list in the test's own
    # directory; nothing here depends on what it holds.
    denylist = root / "secret_globs.txt"
    denylist.write_text("*.pem\n", encoding="utf-8")
    kw.setdefault("secret_globs_file", str(denylist))
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


BOM = b"\xef\xbb\xbf"


def bare_lf(data: bytes) -> int:
    """How many line feeds are not the second half of a CRLF."""
    return data.replace(b"\r\n", b"").count(b"\n")


def test_a_one_line_edit_keeps_the_files_utf8_bom(tmp_path):
    target = tmp_path / "mod.py"
    target.write_bytes(BOM + b"alpha\nbeta\ngamma\n")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="beta", new_string="BETA"),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, result.content
    written = target.read_bytes()
    assert written.startswith(BOM), (
        "the BOM was stripped on the way in and never written back, so the edit rewrote "
        "the first bytes of a file it was only asked to change one line of"
    )
    assert written == BOM + b"alpha\nBETA\ngamma\n"


def test_a_crlf_file_accepts_a_multi_line_quote_as_read_file_shows_it(tmp_path):
    target = tmp_path / "mod.py"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="one\ntwo",
             new_string="ONE\nTWO"),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, (
        "read_file shows this file with LF endings, so the quotation it printed matches "
        f"nothing in the file's own bytes: {result.content}"
    )
    assert target.read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"
    assert bare_lf(target.read_bytes()) == 0, (
        "a bare LF was written into a CRLF file, which mixes the endings"
    )


def test_a_crlf_file_keeps_crlf_after_a_single_line_edit(tmp_path):
    target = tmp_path / "mod.py"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="beta", new_string="BETA"),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, result.content
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_an_lf_file_is_edited_exactly_as_before(tmp_path):
    """The control: nothing about a plain LF file may change, and no BOM may appear."""
    target = tmp_path / "mod.py"
    target.write_bytes(b"alpha\nbeta\ngamma\n")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="beta", new_string="BETA"),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, result.content
    assert "Replaced 1 occurrence" in result.content
    assert target.read_bytes() == b"alpha\nBETA\ngamma\n"


def test_a_file_with_mixed_endings_is_matched_as_it_is(tmp_path):
    """Neither form is the file's own, so today's behaviour stands rather than a guess
    that would rewrite every line the model never quoted."""
    target = tmp_path / "mod.py"
    target.write_bytes(b"one\r\ntwo\nthree\n")
    result = tools.execute_tool(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="one\ntwo",
             new_string="ONE\nTWO"),
        tools.ALL_TOOL_NAMES,
    )
    assert result.is_error
    assert "does not appear" in result.content
    assert target.read_bytes() == b"one\r\ntwo\nthree\n"
