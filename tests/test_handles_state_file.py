"""`Handles` writes a per-handle state file the wait-hook polls (ADR-0126).

The `delegate` tools answer with a handle at once, and a caller that ends its turn hears
nothing more until the person writes. So each handle carries a tiny JSON file on tmpfs that
`wait-hook` (wake.py) polls; this pins what that file says at each step of a run's life. With
`state_dir=None` nothing is written, which is the behaviour every existing caller relies on.
"""

from __future__ import annotations

import asyncio
import json

from claude_delegate_local.handles import Handles, Pause


def _state(tmp_path, handle: str) -> dict:
    return json.loads((tmp_path / f"{handle}.json").read_text())


async def _done() -> dict:
    return {}


async def _ask_then_wait(pause: Pause, release: asyncio.Event) -> dict:
    await pause.ask(["which?"])
    await release.wait()
    return {}


def test_start_writes_a_running_state_file(tmp_path):
    handles = Handles(ttl_seconds=600, state_dir=tmp_path)

    async def go():
        handle = handles.start(_done(), tool="delegate")
        state = _state(tmp_path, handle)
        assert state["handle"] == handle
        assert state["status"] == "running"
        assert state["questions_asked"] == 0
        assert state["questions_shown"] == 0
        assert state["collected"] is False
        await handles.task(handle)  # let the run finish, so asyncio.run has nothing pending

    asyncio.run(go())


def test_a_question_is_written_and_then_read(tmp_path):
    handles = Handles(ttl_seconds=600, state_dir=tmp_path)

    async def go():
        pause = Pause()
        release = asyncio.Event()
        handle = handles.start(_ask_then_wait(pause, release), tool="delegate", pause=pause)
        assert _state(tmp_path, handle)["status"] == "running"

        first = await handles.collect(handle, 5)
        assert first["status"] == "question"
        state = _state(tmp_path, handle)
        assert state["status"] == "question"
        assert state["questions_asked"] == 1
        assert state["questions_shown"] == 1

        handles.answer(handle, "a1")
        assert _state(tmp_path, handle)["status"] == "running"

        release.set()
        assert (await handles.collect(handle, 5))["status"] == "done"

    asyncio.run(go())


def test_a_finished_run_is_done_then_collected(tmp_path):
    handles = Handles(ttl_seconds=600, state_dir=tmp_path)

    async def go():
        handle = handles.start(_done(), tool="delegate")
        await handles.task(handle)
        await asyncio.sleep(0)  # let the done-callback write the state
        state = _state(tmp_path, handle)
        assert state["status"] == "done"
        assert state["collected"] is False

        assert (await handles.collect(handle, 5))["status"] == "done"
        assert _state(tmp_path, handle)["collected"] is True

    asyncio.run(go())


def test_reap_removes_the_state_file(tmp_path):
    handles = Handles(ttl_seconds=0.0, state_dir=tmp_path)

    async def go():
        handle = handles.start(_done(), tool="delegate")
        await handles.task(handle)
        await asyncio.sleep(0.01)  # the finished time is now past the TTL
        assert (tmp_path / f"{handle}.json").exists()
        handles._reap()
        assert not (tmp_path / f"{handle}.json").exists()

    asyncio.run(go())


def test_no_state_dir_writes_nothing(tmp_path):
    handles = Handles(ttl_seconds=600, state_dir=None)

    async def go():
        handle = handles.start(_done(), tool="delegate")
        assert list(tmp_path.glob("*.json")) == []
        await handles.task(handle)

    asyncio.run(go())
