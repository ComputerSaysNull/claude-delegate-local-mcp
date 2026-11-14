"""A search called a file it had only half read absent, and lost the half it did read.

`_search_hits` reads each file with `fh.read(cfg.max_file_read_bytes)` and never records
that the read stopped at the cap. Two things follow from that.

The first is the answer a model acts on. A pattern that occurs only past the cap came back
as "No line matched ... The pattern is absent from everything the path policy lets you read
there" -- which reads as exhaustive, and is the failure the scan cap already had a note
for: a truncated search that reads like a complete one makes the model deny a symbol
exists.

The second is silent and worse: when the cut lands inside a multi-byte UTF-8 character,
`decode_text` returns None and the whole file is dropped, so even the matches *before* the
cap disappear. That is the shape of a file cut mid-character rather than a binary file,
and the two must not be reported the same way.

Named after the bug, per the project's convention.
"""

from pathlib import Path

from claude_delegate_local import tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

# Small enough that the fixtures below straddle it deliberately, and that the cut can be
# placed inside a character instead of between two.
CAP = 40


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


def search(root: Path, pattern: str, *, max_file_read_bytes: int) -> str:
    """Search `root/src`, named explicitly.

    A path resolving to a workspace root is refused as a scope, so the fixture's project is
    a subdirectory of the root: a refusal there would be a red that proves nothing.
    """
    result = tools.execute_tool(
        cfg(root, max_file_read_bytes=max_file_read_bytes),
        call("search_files", pattern=pattern, path=str(root / "src")),
        tools.ALL_TOOL_NAMES,
    )
    assert not result.is_error, result.content
    return result.content


def test_a_match_past_the_cut_is_not_reported_as_absent(tmp_path):
    """The symptom a model meets: an answer it reads as exhaustive.

    The pattern occurs once, on the sixth line, past a 40-byte cap, so a read that stops at
    the cap never sees it. The result must name the file as partly searched, say where the
    read stopped, and must not claim the pattern is absent anywhere.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "past_the_cut.py").write_text(
        "filler line\n" * 5 + "target_symbol = 1\n", encoding="utf-8"
    )

    out = search(tmp_path, pattern="target_symbol", max_file_read_bytes=CAP)

    assert "past_the_cut.py" in out, (
        "the file that was only partly searched is not named, so the caller cannot tell "
        "this answer from an exhaustive one")
    assert str(CAP) in out, "the byte limit the read stopped at is not stated"
    assert "absent" not in out.lower(), (
        "the result still claims the pattern is absent, which is a proof it does not have")


def test_a_cut_through_a_multi_byte_character_still_finds_the_matches_before_it(tmp_path):
    """The second face of the same bug, and the one that loses real results.

    The first line holds a match. The two-byte `é` starts at byte 39, so a 40-byte read
    keeps its lead byte and drops its continuation: the blob is not valid UTF-8, and
    `decode_text` returned None for it, dropping the file whole. Trimming the incomplete
    trailing sequence is what keeps `alpha` findable here.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cut_through_a_character.py").write_text(
        "alpha = 1\n" + "#" * 29 + "é\n" + "beta = 2\n", encoding="utf-8"
    )

    out = search(tmp_path, pattern="alpha", max_file_read_bytes=CAP)

    assert "line 1: alpha = 1" in out, (
        "the match before the cut was lost: a cut inside a character made the whole file "
        "undecodable, so it was skipped as if it were binary")
    assert "cut_through_a_character.py" in out, (
        "the file read only in part is not named, so the caller cannot tell that `beta` "
        "past the cut was never looked at")
