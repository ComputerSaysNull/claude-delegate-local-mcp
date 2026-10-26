"""The `handle` argument tells the model where a handle comes from.

Since #499/#500 a delegating call returns a `handle` only when the run stops to ask a
question (`status: "question"`). `answer` and `cancel_delegation` both take that handle,
so the argument description must say so -- otherwise a model that only ever saw a finished
`delegate` result might expect a handle to be on offer there too. The description is what
lands in the tool's `inputSchema`, so that is where the contract is asserted.
"""

import asyncio

from fastmcp import Client
from test_server import _built


def _handle_description(tool_name: str) -> str:
    async def go() -> str:
        async with Client(_built()) as client:
            tools = {t.name: t for t in await client.list_tools()}
        handle = tools[tool_name].input_schema["properties"]["handle"]
        return handle.get("description") or ""

    return asyncio.run(go())


def test_the_handle_of_answer_says_it_comes_from_a_question() -> None:
    described = " ".join(_handle_description("answer").split())
    assert 'status: "question"' in described


def test_the_handle_of_cancel_delegation_says_it_comes_from_a_question() -> None:
    described = " ".join(_handle_description("cancel_delegation").split())
    assert 'status: "question"' in described
