"""A file over the per-file prefetch cap was skipped whole, so a large source file could
never be prefetched at all (PLAN U.70).

A `files[]` entry may name a line range, so a review can prefetch the part of a file it
cares about and the whole file's size no longer decides whether any of it is inlined. The
refusal, not clamping: a bad range is refused for that entry only, never silently fixed.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastmcp import Client

from claude_delegate_local.config import Config
from claude_delegate_local import server
from claude_delegate_local.context import MARKER_LINE
from test_server import DoubleCache, chat_reply, entry, registry
from wire_double import as_stream

UNPROVEN = (
    "PREFETCH UNPROVEN BY THIS RUN -- this is not a pass. It reads real files through "
    "resolved POSIX paths, and the server runs in WSL. Run it there -- see CONTRIBUTING.md."
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def files_cfg(tmp_path, **over) -> Config:
    globs = tmp_path / "globs.txt"
    globs.write_text(".env\n*secret*\n", encoding="utf-8")
    kw = {
        "workspace_roots": (os.path.realpath(tmp_path),),
        "secret_globs_file": str(globs),
        "respect_gitignore": False,
        "admission_idle_hold": 0.0,
        # Deliberately tight: the whole file is over this, the 10-20 range is not.
        "max_file_tokens": 50,
        "max_total_prefetch_tokens": 1000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def big_file(tmp_path, name: str = "big.py") -> str:
    target = tmp_path / name
    target.write_text(
        "\n".join(f"line {i}" for i in range(1, 201)) + "\n", encoding="utf-8"
    )
    return str(target)


def recording_handler(sent: list):
    def handler(request):
        sent.append(json.loads(request.content))
        return as_stream(chat_reply(content="ok"))

    return handler


def dispatched(config, sent: list, files):
    """Call delegate_readonly over a real MCP session, capturing the request."""
    mcp = server.build(
        config, registry(entry()), DoubleCache(config, recording_handler(sent))
    )

    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "delegate_readonly", {"task": "q", "effort": "inherit", "files": files}
            )
            return getattr(result, "structured_content", None) or result.data

    return asyncio.run(go())


@posix_only
def test_a_large_file_named_with_a_range_is_prefetched_in_part(tmp_path):
    target = big_file(tmp_path)
    sent: list = []
    result = dispatched(
        files_cfg(tmp_path), sent,
        files=[{"path": target, "start_line": 10, "end_line": 20}],
    )

    prompt = json.dumps(sent[0])
    assert "lines 10-20 of 200" in prompt, "the BEGIN header must say it is part of the file"
    assert "10  line 10" in prompt, "the range must be numbered with the file's own numbers"
    assert "20  line 20" in prompt
    assert result["files_read"], "the ranged file was not read"
    assert result["files_read"][0]["path"] == os.path.realpath(target)
    assert result["files_skipped"] == [], f"skipped: {result['files_skipped']}"


@posix_only
def test_a_bad_range_is_refused_while_a_valid_plain_entry_still_prefetches(tmp_path):
    big = big_file(tmp_path, "big.py")
    other = tmp_path / "other.py"
    other.write_text("x = 1\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text("y = 2\n", encoding="utf-8")

    sent: list = []
    result = dispatched(
        files_cfg(tmp_path), sent,
        files=[
            {"path": big, "start_line": 0},
            {"path": str(other), "start_line": 5, "end_line": 2},
            str(good),
        ],
    )

    skipped = result["files_skipped"]
    assert len(skipped) >= 2, f"skipped: {skipped}"
    reasons = " ".join(s["reason"] for s in skipped)
    assert "start_line 0" in reasons, f"start_line 0 was not refused: {reasons}"
    assert "end_line 2 is before start_line 5" in reasons, (
        f"an empty range was not refused: {reasons}"
    )
    # The valid plain entry still prefetched.
    assert [f["path"] for f in result["files_read"]] == [os.path.realpath(good)]
    assert "y = 2" in json.dumps(sent[0])


@posix_only
def test_a_range_that_is_itself_over_the_cap_is_skipped_whole(tmp_path):
    target = big_file(tmp_path)
    sent: list = []
    result = dispatched(
        files_cfg(tmp_path), sent,
        files=[{"path": target, "start_line": 10, "end_line": 200}],
    )

    assert result["files_read"] == []
    assert result["files_skipped"][0]["kind"] == "over_file_budget", (
        f"skipped: {result['files_skipped']}"
    )
    prompt = json.dumps(sent[0])
    assert "line 20" not in prompt, "an over-cap range must not be inlined"


@posix_only
def test_a_plain_string_entry_renders_exactly_as_before(tmp_path):
    target = tmp_path / "plain.py"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")

    sent: list = []
    result = dispatched(files_cfg(tmp_path), sent, files=[str(target)])

    prompt = json.dumps(sent[0])
    assert f"--- BEGIN FILE {os.path.realpath(target)} ---" in prompt
    assert "1  first" in prompt
    assert "3  third" in prompt
    assert result["files_skipped"] == []

@posix_only
def test_the_ranged_header_is_a_marker_a_file_cannot_forge_unescaped(tmp_path):
    """A file line shaped like a marker is escaped only if `MARKER_LINE` matches it, and
    that needs the dashes last. So the range goes inside the markers: a suffix after them
    would be a boundary a prefetched file could print and have taken for real.
    """
    target = big_file(tmp_path)
    sent: list = []
    dispatched(
        files_cfg(tmp_path), sent,
        files=[{"path": target, "start_line": 10, "end_line": 20}],
    )

    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)

    headers = [ln for s in strings(sent[0]) for ln in s.splitlines()
               if "BEGIN FILE" in ln and "(lines 10-20 of 200)" in ln]
    assert headers, "no ranged header in the prompt"
    assert all(MARKER_LINE.match(h) for h in headers), (
        f"a ranged header the escaper would not recognise: {headers}")
