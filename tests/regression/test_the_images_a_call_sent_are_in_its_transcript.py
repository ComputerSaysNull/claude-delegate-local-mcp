"""A tool result that carried images recorded none of them in the transcript.

`ToolCallRecord.as_json()` wrote `result_bytes`/`result_lines` but never how many
images went with the result, so an eight-tile screenshot read back as "212 B · 1 line"
-- the one fact an operator replaying a run wants, that the model was handed a picture
and how big, was gone. `Prefetch.accounting()` had the same hole for the images it put
in the first user message: a `files_read` row named the bytes but not how many images
they were. The fix adds two optional integer fields to transcript format 1.13 --
`result_images` on a tool_call record and `images` on a files_read entry, both absent
at zero -- and pins them as integers in the schema. These tests are red until the
fields exist; the negative controls stay green so that the missing field, and only it,
is what fails.
"""

import asyncio
import copy
import json
import os
from pathlib import Path

import jsonschema
import pytest

from claude_delegate_local import loop, tools, transcript
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.context import prefetch
from claude_delegate_local.paths import ResolvedPath
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist

SCHEMA_PATH = Path(transcript.__file__).with_name("transcript.schema.json")
SAMPLES = sorted((Path(__file__).resolve().parent.parent / "transcript_samples").glob("*.jsonl"))


def cfg(**over) -> Config:
    """The loop's config: overflow handling off, as the scripted-turn tests need it."""
    kw = {"workspace_roots": (".",), "context_overflow_enabled": False}
    kw.update(over)
    if "dispatch_timeout" in kw and "stall_timeout" not in kw:
        kw["stall_timeout"] = kw["dispatch_timeout"]
    return Config(**kw)  # type: ignore[arg-type]


def prefetch_cfg(**over) -> Config:
    """The prefetch's config: the resolved files are outside the CWD, so name no roots."""
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1",
          "context_window_defaulted": True}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


# --- scripted replies (copied from test_agentic_loop.py) -------------------------------


def says(text: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


def wants(*calls: tuple[str, dict], text: str = "") -> CanonicalResponse:
    blocks: list = [TextBlock(text)] if text else []
    blocks += [
        ToolUseBlock(id=f"call-{i}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    ]
    return CanonicalResponse(
        content=tuple(blocks),
        finish_reason="tool_calls",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


class ScriptedTurns:
    """Returns one scripted reply per call, and records every request it was given."""

    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)
        self.requests: list = []

    async def complete(self, request, *, on_token=None):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


def run(backend, *, allowed: frozenset[str], **over):
    kw = {"max_turns": 5}
    kw.update(over)
    return asyncio.run(
        loop.run_agentic_loop(
            kw.pop("cfg", cfg()),
            kw.pop("entry", entry()),
            backend,
            loop.Delegation(kw.pop("task", "do the thing"), images=kw.pop("images", ())),
            allowed=allowed,
            **kw,
        )
    )


def img() -> ImageBlock:
    """One tiny literal image, built from bytes rather than decoded by Pillow."""
    return ImageBlock("image/png", b"\x89PNG fake", 1, 1)


@pytest.fixture
def text_tool():
    """A real, cacheable tool that answers with text, removed after the test."""
    added = {
        "echo": tools.RegisteredTool(
            spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object"}),
            handler=lambda cfg_, args: "echoed",
            cacheable=True,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        tools.REGISTRY.pop("echo", None)


# --- a tool result that carried images ------------------------------------------------


def register_pic(monkeypatch, images: tuple) -> None:
    """Register the `pic` tool and make it answer with `images`, so the image-result
    tests share the setup and the monkeypatch of `execute_tool`. The caller pops the
    tool from the registry when it is done."""
    added = {
        "pic": tools.RegisteredTool(
            spec=ToolSpec(name="pic", description="pic", input_schema={"type": "object"}),
            handler=lambda cfg_, args: "an image",
            cacheable=True,
        )
    }
    tools.REGISTRY.update(added)
    real = loop.execute_tool

    def with_image(cfg_, call, allowed, policy=None, seen=None):
        if call.name == "pic":
            return ToolResultBlock(
                tool_use_id=call.id, content="an image", images=images,
            )
        return real(cfg_, call, allowed, policy, seen)

    monkeypatch.setattr(loop, "execute_tool", with_image)


def test_a_tool_result_carrying_several_images_records_its_count(monkeypatch):
    """The fix. The transcript should say how many images the result carried, so a run
    that showed the model a tiled screenshot reads as more than a single text line."""
    register_pic(monkeypatch, (img(), img(), img()))
    try:
        backend = ScriptedTurns(wants(("pic", {})), says("done"))
        result = run(backend, allowed=frozenset({"pic"}), entry=entry(max_images=8),
                     diagnostics=True)
    finally:
        tools.REGISTRY.pop("pic", None)

    record = result.diagnostics[0].tool_calls[0]
    assert record.as_json()["result_images"] == 3


def test_a_result_with_no_images_has_no_result_images_key(text_tool):
    """Negative control. A text result is the commonest case, and a zero that was written
    would read as "measured empty" rather than "no image was sent"."""
    backend = ScriptedTurns(wants(("echo", {})), says("done"))
    result = run(backend, allowed=frozenset({"echo"}), diagnostics=True)

    record = result.diagnostics[0].tool_calls[0]
    assert record.outcome == "ran"
    assert "result_images" not in record.as_json()


def test_a_refused_image_records_an_error_and_no_result_images_key(monkeypatch):
    """Negative control. The model never saw the image, so the record is the refusal the
    model was handed -- which has no image -- and must not claim one."""
    register_pic(monkeypatch, (img(),))
    try:
        backend = ScriptedTurns(wants(("pic", {})), says("done"))
        result = run(backend, allowed=frozenset({"pic"}), entry=entry(max_images=0),
                     diagnostics=True)
    finally:
        tools.REGISTRY.pop("pic", None)

    record = result.diagnostics[0].tool_calls[0]
    assert record.outcome == "error"
    assert "result_images" not in record.as_json()


# --- prefetched images in the accounting ----------------------------------------------


def write(tmp_path, name: str, data: bytes | str) -> ResolvedPath:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return ResolvedPath(given=str(p), posix=os.path.realpath(p))


def _screenshot(size) -> bytes:
    """A white PNG of the given size, large enough to be tiled."""
    import io

    from PIL import Image as PILImage

    im = PILImage.new("RGB", size, (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_a_prefetched_tiled_image_records_its_tile_count_in_the_accounting(tmp_path):
    """The fix. The files_read row for a tiled image says how many tiles went with it,
    and a text file prefetched beside it says nothing of the sort."""
    img_item = write(tmp_path, "shot.png", _screenshot((3840, 2160)))
    text_item = write(tmp_path, "a.py", "x = 1\n")
    result = prefetch(prefetch_cfg(), (img_item, text_item), max_images=8)
    assert result.skips == ()

    entry = next(e for e in result.files if e.path == img_item.posix)
    assert entry.image is not None and len(entry.image.tiles) > 1

    rows = {row["path"]: row for row in result.accounting()["files_read"]}
    assert rows[img_item.posix]["images"] == len(entry.image.tiles)
    assert "images" not in rows[text_item.posix]


# --- the schema pins both fields as integers ------------------------------------------


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def errors(event: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema())
    return [e.message for e in validator.iter_errors(event)]


def sample_events() -> list[dict]:
    return [json.loads(line) for p in SAMPLES
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def first(kind: str) -> dict:
    return copy.deepcopy(next(e for e in sample_events() if e["t"] == kind))


def test_the_schema_accepts_an_integer_result_images_count():
    """The field exists and is an integer, so a real count validates."""
    turn = first("turn")
    turn["tool_calls"][0]["result_images"] = 8
    assert errors(turn) == []


def test_the_schema_rejects_a_string_result_images_count():
    """The field is typed. A string "8" would pass a schema that only knew the key."""
    turn = first("turn")
    turn["tool_calls"][0]["result_images"] = "8"
    assert errors(turn)


def test_the_schema_accepts_an_integer_images_count_on_a_files_read():
    """The prefetch half, validated the same way."""
    start = first("start")
    start["files_read"][0]["images"] = 8
    assert errors(start) == []


def test_the_schema_rejects_a_string_images_count_on_a_files_read():
    """The typed counterpart: a string count is refused, not tolerated."""
    start = first("start")
    start["files_read"][0]["images"] = "8"
    assert errors(start)
