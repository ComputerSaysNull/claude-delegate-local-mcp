"""A transcript named the image it sent, but kept no copy of it.

`result_images` counts what the model was handed and `images` counts a prefetched file's
tiles, and both are only numbers: the bytes lived on disk, so rewriting a screenshot after
a run made the transcript unable to say what the model actually saw. The fix keeps a copy
of every image sent under `<stem>.images/` beside the stream, named by the sha256 of the
exact bytes on the wire, and names it from the tool call or files_read row that sent it.
These tests are red until the copies, the two `*_image_files` fields, the byte cap and
format 1.17 exist.
"""

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path

from fastmcp import Client
from PIL import Image as PILImage

from wire_double import as_stream
from test_server import (
    answered,
    DoubleCache,
    cfg,
    chat_reply,
    entry,
    registry,
    tool_call_reply,
)

from claude_delegate_local import server, transcript

# A denylist of the test's own: the real one need not be readable here, and a relative
# setting would resolve against the repository rather than the sandbox.
DENYLIST = "# the test's own denylist\n.git/**\n"


def _png(colour=(255, 255, 255)) -> bytes:
    """A small white (or other) PNG, built rather than checked in."""
    image = PILImage.new("RGB", (8, 8), colour)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _config(tmp_path: Path, **over):
    globs = tmp_path / "secret_globs.txt"
    globs.write_text(DENYLIST, encoding="utf-8")
    kw = {
        "transcript_dir": str(tmp_path / "transcripts"),
        "workspace_roots": (str(tmp_path),),
        "secret_globs_file": str(globs),
        "max_turns_default": 4,
        "max_turns_default_writing": 4,
    }
    kw.update(over)
    return cfg(**kw)


def _drive(tmp_path: Path, handler, tool: str, args: dict, **over):
    """Run one delegation against a transport double, and return its stream and events."""
    config = _config(tmp_path, **over)
    mcp = server.build(config, registry(entry(max_images=8)), DoubleCache(config, handler))

    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            return await answered(client, tool, args)

    asyncio.run(go())

    directory = Path(config.transcript_dir)
    streams = list(directory.glob("*.jsonl"))
    assert len(streams) == 1, f"expected one stream, found {[p.name for p in streams]}"
    events = [
        json.loads(line)
        for line in streams[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return streams[0], events


def _image_folder(stream: Path) -> Path:
    """`<stem>.images/`, the folder the copies go in for this stream."""
    return Path(str(stream)[: -len(".jsonl")] + ".images")


def _files_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []


def _read_file_calls(events: list[dict]) -> list[dict]:
    return [
        call
        for event in events
        if event.get("t") == "turn"
        for call in event.get("tool_calls") or []
        if call.get("name") == "read_file"
    ]


def _reads_once(path: Path, received: list[str]):
    def handler(request):
        body = json.loads(request.content)
        if any(m.get("role") == "tool" for m in body.get("messages", [])):
            received.extend(_image_data_uris(request))
            return as_stream(chat_reply(content="it is a white square"))
        return as_stream(tool_call_reply("read_file", {"path": str(path)}))

    return handler


def _image_data_uris(request) -> list[str]:
    """Every image part's data URI in the request, in order.

    What the fake backend was actually sent, which is the only honest answer to "what did
    the model see": the file on disk can be rewritten afterwards, and a tile's bytes are
    re-encoded from the decoded image rather than copied from it.
    """
    try:
        body = json.loads(request.content or b"{}")
    except ValueError:
        return []
    return [
        part["image_url"]["url"]
        for message in body.get("messages", [])
        for part in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def _decode(uri: str) -> bytes:
    """The bytes behind one data URI, which are what the wire carried."""
    head, _, payload = uri.partition(",")
    assert head.startswith("data:") and head.endswith(";base64"), head
    return base64.b64decode(payload)


def _reads_twice(path: Path):
    def handler(request):
        body = json.loads(request.content)
        answered = [m for m in body.get("messages", []) if m.get("role") == "tool"]
        if len(answered) >= 2:
            return as_stream(chat_reply(content="the same picture twice"))
        return as_stream(tool_call_reply("read_file", {"path": str(path)}))

    return handler


def test_a_rewritten_image_still_shows_what_the_model_was_sent(tmp_path):
    """The failure this prevents: a screenshot read during a run was overwritten
    afterwards, and the transcript could only say how many images there had been, not
    what they looked like. The copy is named by the sha256 of the bytes sent, and those
    are the original bytes, not whatever is on disk now."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    received: list[str] = []
    stream, events = _drive(
        tmp_path, _reads_once(shot, received), "delegate", {"task": "look at the screenshot"},
    )
    calls = _read_file_calls(events)
    assert calls, [e.get("t") for e in events]

    names = calls[0]["result_image_files"]
    assert len(names) == 1, names

    # What the wire carried, not the file: the copy must be of the bytes the model was
    # sent, and a tile is re-encoded rather than read back from disk.
    assert len(received) == 1, received
    sent = _decode(received[0])

    # The rewrite happens after the run, and the copy must survive it.
    shot.write_bytes(_png(colour=(0, 0, 0)))

    folder = _image_folder(stream)
    copy = folder / names[0]
    assert copy.is_file(), (
        f"{names[0]} is not in {folder}: {[p.name for p in _files_in(folder)]}"
    )
    kept = copy.read_bytes()
    assert hashlib.sha256(kept).hexdigest() == names[0].rsplit(".", 1)[0], names[0]
    assert kept == sent
    assert hashlib.sha256(kept).hexdigest() != hashlib.sha256(shot.read_bytes()).hexdigest()


def test_a_prefetched_image_names_the_copy_beside_the_stream(tmp_path):
    """A file named in `files[]` is sent without any tool call, so its `files_read` row
    is the only place a reader can be pointed at what the model was given."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    received: list[str] = []

    def handler(request):
        received.extend(_image_data_uris(request))
        return as_stream(chat_reply(content="seen"))

    stream, events = _drive(
        tmp_path,
        handler,
        "delegate",
        {"task": "look at the screenshot", "files": [str(shot)]},
    )
    start = next(e for e in events if e.get("t") == "start")
    row = start["files_read"][0]
    names = row["image_files"]

    assert len(names) == 1, names
    # The image the first request carried, which is what the model was handed.
    assert len(received) == 1, received
    sent = _decode(received[0])
    copy = _image_folder(stream) / names[0]
    assert copy.is_file(), sorted(p.name for p in _files_in(_image_folder(stream)))
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == names[0].rsplit(".", 1)[0]
    assert copy.read_bytes() == sent


def test_the_same_image_sent_twice_is_stored_once(tmp_path):
    """Negative control for the hash: naming copies by anything unique per call would put
    two identical images in the folder and make dedup untestable. Same bytes, one file,
    the name listed on both calls."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    stream, events = _drive(
        tmp_path, _reads_twice(shot), "delegate", {"task": "look twice"},
    )
    groups = [call["result_image_files"] for call in _read_file_calls(events)]
    flat = [name for group in groups for name in group]

    assert len(groups) == 2, groups
    assert len(flat) == 2, flat
    assert flat[0] == flat[1], flat
    assert [p.name for p in _files_in(_image_folder(stream))] == [flat[0]]


def test_a_cap_too_small_for_the_image_keeps_nothing(tmp_path):
    """The cap is a total per stream, and an image that does not fit under it keeps none
    of its bytes -- and says so by leaving the key out, not by writing an empty list,
    which would read as "measured, nothing to send"."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    stream, events = _drive(
        tmp_path,
        _reads_once(shot, []),
        "delegate",
        {"task": "look at the screenshot"},
        transcript_image_cap_bytes=1,
    )
    calls = _read_file_calls(events)
    assert calls, [e.get("t") for e in events]
    assert "result_image_files" not in calls[0], calls[0]
    assert _files_in(_image_folder(stream)) == []


def test_the_transcript_format_is_the_one_that_carries_image_copies():
    """A reader branching on the format has to be told that `*_image_files` exists."""
    assert transcript.FORMAT == "1.18"
