"""A delegation may ask its caller more than once (ADR-0125, superseding ADR-0118's limit).

Two halves, each of which alone would leave a second question stranded: the loop has to
put it to the caller rather than answer it with a refusal, and a `collect` that is already
waiting has to wake for it, not only for the run's first question.
"""

import asyncio
import time

from claude_delegate_local.handles import Handles, Pause
from test_ask_caller import ScriptedTurns, results_in, run, says, wants


def test_a_second_question_reaches_the_caller():
    called: list = []

    async def answer(questions):
        called.append(list(questions))
        return f"answer {len(called)}"

    backend = ScriptedTurns(
        wants(("ask_caller", {"questions": ["first?"]})),
        wants(("ask_caller", {"questions": ["second?"]})),
        says("done"),
    )
    result = run(backend, ask=answer)
    assert called == [["first?"], ["second?"]]
    second = results_in(backend.requests[2])
    assert len(second) == 1
    assert "answer 2" in second[0].content
    assert result.response.text == "done"


def test_a_waiting_collect_wakes_for_the_second_question():
    """The run asks, is answered, and asks again 0.2s later. A `collect` given 5s must
    return the second question at once, not when its wait runs out."""
    async def go():
        pause = Pause()
        handles = Handles()

        async def work():
            await pause.ask(["first?"])
            await asyncio.sleep(0.2)
            await pause.ask(["second?"])
            return {}

        handle = handles.start(work(), tool="delegate", pause=pause)
        first = await handles.collect(handle, 5)
        assert first["status"] == "question", first
        handles.answer(handle, "a1")
        started = time.monotonic()
        second = await handles.collect(handle, 5)
        waited = time.monotonic() - started
        assert second["status"] == "question", second
        assert second["questions"] == ["second?"], second
        assert waited < 2.5, f"collect saw the second question only after {waited:.1f}s"
        handles.answer(handle, "a2")
        assert (await handles.collect(handle, 5))["status"] == "done"

    asyncio.run(go())
