"""A transcript that could not be written failed a delegation whose work was done.

`transcript`'s module docstring makes one promise to the dispatch path: nothing here may
raise into it, because a full disk or a strange value must not fail work that already
succeeded. `Stream._put` guarded only `OSError`, but the two ways its write can fail are
neither. `json.dumps` refuses a non-string dict key with `TypeError` -- a call row is
carried verbatim, so a caller's key reaches it -- and the UTF-8 write refuses a lone
surrogate with `UnicodeEncodeError`, which `ensure_ascii=False` is exactly what makes
reachable. Either one escaped `_put` and took the delegation with it.

The fix is the guard the record already had, widened: any `Exception` means a broken
stream, marked once and never written again. The file is also opened with
`errors="backslashreplace"`, so a lone surrogate is written escaped rather than raising --
it is the one failure that has a faithful rendering, and losing the event would lose a
turn's reply. `_keep` gets the same widened guard, returning `()` as its own failure case.
"""

import json
from types import SimpleNamespace

from claude_delegate_local import transcript
from claude_delegate_local.backends.base import ImageBlock


def test_a_non_string_key_does_not_raise_out_of_the_stream(tmp_path):
    """`tools` carries a turn's call rows verbatim, so a complex key reaches `json.dumps`.

    The event is lost -- there is no rendering of `{1j: 1}` the module promises -- but the
    delegation must not be, and the stream is broken once rather than retried per turn.
    """
    path = tmp_path / "stream.jsonl"
    stream = transcript.Stream(path)

    stream.tools(turn=1, of_turns=1, tool_calls=[{1j: 1}])

    assert stream._broken is True
    # Once only: the next event must not raise either, and must not be written.
    stream.tools(turn=2, of_turns=1, tool_calls=[{1j: 1}])
    assert path.read_text(encoding="utf-8") == ""


def test_a_lone_surrogate_is_written_escaped_rather_than_breaking_the_stream(tmp_path):
    """A reply can hold a lone surrogate, and it has an honest rendering: escaped.

    So this is not a broken stream but a written event, and one a reader parses back to
    the character the model produced. `_broken` staying false is the assertion that
    separates the two failures: the escaped write must not be treated as a failure.
    """
    path = tmp_path / "stream.jsonl"
    stream = transcript.Stream(path)

    stream.partial(turn=1, reasoning="", answer="\ud800")

    assert stream._broken is False
    (line,) = path.read_text(encoding="utf-8").splitlines()
    assert "\\ud800" in line
    assert json.loads(line)["answer"] == "\ud800"


class _Call:
    """A tool call row whose `as_json` is all `turn` asks of it, plus one image sent."""

    sent_images = (ImageBlock("image/png", b"\x89PNG\r\n\x1a\n", 1, 1),)

    def as_json(self):
        return {"name": "read_file"}


def test_an_image_that_could_not_be_kept_does_not_raise_out_of_the_stream(
    tmp_path, monkeypatch,
):
    """`_keep`'s guard was `OSError` too, and keeping a copy is not the only thing it does.

    A failure it does not name must cost the copies and nothing else: the turn event is
    still written, with the image field left out rather than naming a subset.
    """
    path = tmp_path / "stream.jsonl"
    stream = transcript.Stream(path)

    def broken_write(self, target, data):
        raise TypeError("not the failure OSError covers")

    monkeypatch.setattr(transcript.Stream, "_write_image", broken_write)
    diagnostic = SimpleNamespace(turn=1, model=None, tool_calls=[_Call()])

    stream.turn(diagnostic, "an answer")

    assert stream._broken is False
    (line,) = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    assert event["t"] == "turn"
    assert "result_image_files" not in event["tool_calls"][0]
