"""A cheap cache miss on a small prompt was counted as the engine rebuilding.

The rebuild rule exists for a prompt the engine dropped between turns, which costs a run
the whole prefix. Below a certain size a miss costs almost nothing and happens on an idle
cluster with no load at all, so counting it inflates the gauge: a run whose first turn was
2,110 tokens came back on turn two with `cached_tokens: 0` and was booked as a rebuild.

Over five days of streams, turn two missed after a first turn under 20,000 tokens in 11 of
136 runs, and after one of 20,000 or more in 0 of 53. So the rule has a floor, and the
twin here keeps it from switching the counter off: at 25,000 tokens a rebuilt prompt is
still reported.

Named after the bug, per the project's convention.
"""

import pytest

from test_a_rebuilt_prompt_left_no_trace_in_backend_status import (
    ScriptedTurns,
    answer,
    run,
    tool_call,
)
from test_a_rebuilt_prompt_left_no_trace_in_backend_status import (
    echo_tool,  # noqa: F401 -- a fixture, requested by name below
)


# --- a small prompt that missed is not a rebuild ----------------------------------------


@pytest.mark.usefixtures("echo_tool")
def test_a_miss_on_a_small_prompt_is_not_a_rebuild():
    """The red. A 2,110-token first turn and a miss on turn two is a cheap miss."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(2_110, cached_tokens=2_000),
        answer("done", 3_000, cached_tokens=0),
    )
    result = run(backend, on_rebuild=on_rebuild)

    assert result.response.text == "done"
    assert seen == [], (
        f"a miss on a prompt grown from 2,110 tokens was reported as a rebuild: {seen}"
    )


@pytest.mark.usefixtures("echo_tool")
def test_a_rebuilt_prompt_at_the_floor_is_still_reported():
    """The negative control: the floor must not quietly switch the counter off."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(25_000, cached_tokens=24_000),
        answer("done", 26_000, cached_tokens=0),
    )
    result = run(backend, on_rebuild=on_rebuild)

    assert result.response.text == "done"
    assert seen == [26_000], (
        f"a 26,000-token prompt that prefilled with nothing cached reported {seen}"
    )


@pytest.mark.usefixtures("echo_tool")
@pytest.mark.parametrize("first_turn", [19_999, 20_000])
def test_the_floor_is_where_the_measured_misses_stop(first_turn):
    """19,999 is under the floor and 20,000 is on it, so the split is the measured one."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(first_turn, cached_tokens=first_turn - 1_000),
        answer("done", first_turn + 1_000, cached_tokens=0),
    )
    run(backend, on_rebuild=on_rebuild)

    if first_turn < 20_000:
        assert seen == [], f"{first_turn} tokens was counted as a rebuild: {seen}"
    else:
        assert seen == [first_turn + 1_000], f"{first_turn} tokens was not counted: {seen}"
