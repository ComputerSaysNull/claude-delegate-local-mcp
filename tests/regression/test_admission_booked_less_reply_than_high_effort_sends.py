"""Admission booked `cfg.max_tokens` as the reply while the dispatch sent the effort floor.

`run_delegation` sizes the gate's reservation from `cfg.max_tokens` alone, but the budget
actually sent comes from `resolve_max_tokens`, which at effort 'high' or 'max' raises it to
`cfg.thinking_max_tokens_floor` -- 131,072 against a default 65,536, so a high-effort
delegation is admitted against half the reply budget it then sends. `thinking_max_tokens_floor`
says so itself: "Admission accounting must use the retry's size."
"""

import asyncio
from contextlib import asynccontextmanager

from fastmcp import Client

from test_server import DoubleCache, answered, cfg, chat_handler, entry, registry

from claude_delegate_local import server
from claude_delegate_local.context import estimate_text_tokens

TASK = "name the module that defines the admission gate"


def booked(monkeypatch, config, **args) -> int:
    """The token figure one delegation hands the gate, captured where it is admitted."""
    seen: list[int] = []

    class Recording(server.Admission):
        @asynccontextmanager
        async def admit(self, *a, **kw):
            seen.append(a[0])
            async with super().admit(*a, **kw) as lease:
                yield lease

    monkeypatch.setattr(server, "Admission", Recording)
    mcp = server.build(config, registry(entry()), DoubleCache(config, chat_handler()))

    async def go():
        async with Client(mcp) as client:
            return await answered(client, "delegate", {"task": TASK, **args})

    asyncio.run(go())
    assert len(seen) == 1, f"one delegation handed the gate {len(seen)} figures"
    return seen[0]


def reply_part(monkeypatch, config, **args) -> int:
    """The reservation's reply share: the task names no files and no agent adds a body, so
    the prompt half is this task's own text and what is left is the reply budget."""
    return booked(monkeypatch, config, **args) - estimate_text_tokens(config, TASK)


def test_a_high_effort_delegation_books_the_floor_it_will_send(tmp_path, monkeypatch):
    """The bug. `resolve_max_tokens` sends the floor here, and admission must reserve it."""
    config = cfg(slots_dir=str(tmp_path))

    reply = reply_part(monkeypatch, config, effort="high")

    assert reply == config.thinking_max_tokens_floor, (
        f"a high-effort delegation is booked for {reply} reply tokens but sends "
        f"{config.thinking_max_tokens_floor}: the gate is reserving cfg.max_tokens "
        f"({config.max_tokens}) rather than the budget `resolve_max_tokens` hands the model"
    )


def test_a_low_effort_delegation_books_the_configured_cap(tmp_path, monkeypatch):
    """Control. Below the floor the reply is `cfg.max_tokens`, and that is unchanged."""
    config = cfg(slots_dir=str(tmp_path))

    reply = reply_part(monkeypatch, config, effort="low")

    assert reply == config.max_tokens
    assert reply < config.thinking_max_tokens_floor, "the control is not below the floor"


def test_an_explicit_budget_is_booked_as_given_at_high_effort(tmp_path, monkeypatch):
    """Control. An explicit number is not raised by the floor, so it books as it sends."""
    config = cfg(slots_dir=str(tmp_path))

    reply = reply_part(monkeypatch, config, effort="high", max_tokens=8192)

    assert reply == 8192, (
        f"max_tokens=8192 at high effort booked {reply}: an explicit budget is not raised"
    )
