"""An edit landed on a file changed since the model last read it.

`edit_file` matches `old_string` against the file's *current* bytes, so a file that
changed after the model read it -- a `run_bash` command, the operator -- still edits
cleanly if the quoted text survives the change. The model edits a file it no longer
knows. The fix records, per delegation, the sha256 of each file's bytes as the model
last read or the server last wrote them, and refuses an `edit_file` whose current bytes
differ from that record, until the file is read again.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from claude_delegate_local import context, tools
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config

from test_agentic_loop import ScriptedTurns, results_in, run, says, wants

UNPROVEN = (
    "LAYER 1 UNPROVEN BY THIS RUN -- this is not a pass. Resolving a real path needs a "
    "POSIX filesystem and the server runs in WSL, so every case that actually opens a "
    "file is proven there. Run: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest "
    "tests/regression/test_an_edit_landed_on_a_file_changed_since_it_was_read.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def call(name: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id="call-1", name=name, input=dict(args))


def _conf(tmp_path: Path, **over) -> Config:
    return cfg(tmp_path, **over)


def _seen() -> tools.SeenFiles:
    return tools.SeenFiles()


def _execute(conf: Config, call_: ToolUseBlock, seen: tools.SeenFiles | None):
    return tools.execute_tool(conf, call_, tools.ALL_TOOL_NAMES, seen=seen)


def _loop_run(backend, *, allowed, conf, prefetched=()):
    return run(backend, allowed=allowed, cfg=conf, prefetched=prefetched)


def _all_results(backend):
    return [r for req in backend.requests for r in results_in(req)]


class ChangeBetweenTurns(ScriptedTurns):
    """A scripted backend that rewrites a file outside the server before an edit turn."""

    def __init__(self, *replies, target: Path, changed: str) -> None:
        super().__init__(*replies)
        self.target = target
        self.changed = changed
        self.rewrote = False

    async def complete(self, request, *, on_token=None):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        reply = self.replies.pop(0)
        if not self.rewrote and any(
            isinstance(b, ToolUseBlock) and b.name == "edit_file" for b in reply.content
        ):
            self.target.write_text(self.changed, encoding="utf-8")
            self.rewrote = True
        return reply


# --- the refusal ------------------------------------------------------------------------


@posix_only
def test_an_edit_after_an_outside_change_is_refused(tmp_path):
    """The bug. The quoted line still matches, but the file is no longer what was read."""
    target = tmp_path / "mod.py"
    original = "x = 1\ny = 2\n"
    changed = "x = 999\ny = 2\n"
    target.write_text(original, encoding="utf-8")
    seen = _seen()
    first = _execute(
        cfg(tmp_path), call("read_file", path=str(target)), seen
    )
    assert not first.is_error

    # A change outside the server, keeping the quoted line.
    target.write_text(changed, encoding="utf-8")
    result = _execute(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="y = 2", new_string="y = 3"),
        seen,
    )
    assert result.is_error
    assert "changed since" in result.content
    assert "read" in result.content
    assert target.read_text(encoding="utf-8") == changed


# --- negative controls ------------------------------------------------------------------


@posix_only
def test_a_read_edit_edit_without_a_reread_both_succeed(tmp_path):
    """The model's own edits must not trip the guard: each records the bytes it wrote."""
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    seen = _seen()
    first = _execute(cfg(tmp_path), call("read_file", path=str(target)), seen)
    assert not first.is_error
    second = _execute(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2"),
        seen,
    )
    assert not second.is_error
    third = _execute(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 2", new_string="x = 3"),
        seen,
    )
    assert not third.is_error
    assert target.read_text(encoding="utf-8") == "x = 3\n"


@posix_only
def test_an_edit_with_no_earlier_read_succeeds(tmp_path):
    """No record means behave exactly as today: the change is not refused."""
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = _execute(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2"),
        _seen(),
    )
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "x = 2\n"


@posix_only
def test_a_reread_after_an_outside_change_allows_the_edit(tmp_path):
    """Read again and the model knows the file: the same edit is then allowed."""
    target = tmp_path / "mod.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8")
    seen = _seen()
    assert not _execute(cfg(tmp_path), call("read_file", path=str(target)), seen).is_error
    target.write_text("x = 999\ny = 2\n", encoding="utf-8")
    assert not _execute(cfg(tmp_path), call("read_file", path=str(target)), seen).is_error
    result = _execute(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="y = 2", new_string="y = 3"),
        seen,
    )
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "x = 999\ny = 3\n"


@posix_only
def test_a_write_then_edit_with_no_read_in_between_succeeds(tmp_path):
    """A write records the bytes, so an edit right after it is not a change."""
    target = tmp_path / "mod.py"
    seen = _seen()
    assert not _execute(
        cfg(tmp_path), call("write_file", path=str(target), content="x = 1\n"), seen
    ).is_error
    result = _execute(
        cfg(tmp_path),
        call("edit_file", path=str(target), old_string="x = 1", new_string="x = 2"),
        seen,
    )
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "x = 2\n"


# --- the loop ---------------------------------------------------------------------------


@posix_only
def test_the_loop_refuses_an_edit_of_a_file_changed_between_turns(tmp_path):
    """A change outside the server, between the read turn and the edit turn."""
    target = tmp_path / "mod.py"
    original = "x = 1\ny = 2\n"
    changed = "x = 999\ny = 2\n"
    target.write_text(original, encoding="utf-8")
    backend = ChangeBetweenTurns(
        wants(("read_file", {"path": str(target)})),
        wants(("edit_file", {"path": str(target), "old_string": "y = 2",
                             "new_string": "y = 3"})),
        says("done"),
        target=target, changed=changed,
    )
    _loop_run(
        backend,
        allowed=frozenset({"read_file", "edit_file"}),
        conf=_conf(tmp_path),
    )
    edit_result = _all_results(backend)[1]
    assert edit_result.is_error
    assert "changed since" in edit_result.content
    assert target.read_text(encoding="utf-8") == changed


def _entry(real: str, given: str, text: str, nbytes: int) -> context.FileEntry:
    return context.FileEntry(
        path=real, given=given, text=text, nbytes=nbytes, est_tokens=10,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


@posix_only
def test_a_prefetched_file_changed_before_the_first_edit_is_refused(tmp_path):
    """A prefetched file is seeded as seen, so a change before the first edit refuses."""
    target = tmp_path / "mod.py"
    original = "x = 1\ny = 2\n"
    changed = "x = 999\ny = 2\n"
    target.write_text(original, encoding="utf-8")
    real = os.path.realpath(str(target))
    prefetched = (
        _entry(real, str(target), original, len(original.encode("utf-8"))),
    )
    backend = ChangeBetweenTurns(
        wants(("edit_file", {"path": str(target), "old_string": "y = 2",
                             "new_string": "y = 3"})),
        says("done"),
        target=target, changed=changed,
    )
    _loop_run(
        backend,
        allowed=frozenset({"edit_file"}),
        conf=_conf(tmp_path),
        prefetched=prefetched,
    )
    edit_result = _all_results(backend)[0]
    assert edit_result.is_error
    assert "changed since" in edit_result.content
    assert target.read_text(encoding="utf-8") == changed


# --- the wiring -------------------------------------------------------------------------


@posix_only
def test_the_server_hands_the_prefetch_to_the_loop(tmp_path, monkeypatch):
    """The loop test above passes `prefetched` itself, so a server that dropped the
    hand-off would pass it while every prefetched file went unguarded. This goes in
    through the MCP tool and reads what reached the loop."""
    import asyncio

    from fastmcp import Client

    from claude_delegate_local import server
    from test_server import DoubleCache, chat_reply, entry, registry
    from test_server import cfg as server_cfg
    from wire_double import as_stream

    target = tmp_path / "mod.py"
    target.write_bytes(b"x = 1\n")
    reached: dict = {}

    async def fake_loop(*args, **kwargs):
        reached.update(kwargs)
        raise RuntimeError("stopped at the loop on purpose")

    monkeypatch.setattr(server, "run_agentic_loop", fake_loop)
    config = server_cfg(workspace_roots=(str(tmp_path),), respect_gitignore=False)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, lambda _r: as_stream(chat_reply(content="x"))))

    async def go():
        async with Client(mcp) as client:
            await client.call_tool(
                "delegate_readonly",
                {"task": "t", "effort": "inherit", "files": [str(target)]},
                raise_on_error=False,
            )

    asyncio.run(go())
    handed = reached.get("prefetched", ())
    assert [e.sha256 for e in handed] == [hashlib.sha256(b"x = 1\n").hexdigest()]
