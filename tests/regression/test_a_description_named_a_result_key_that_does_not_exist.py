"""A description told the model to read a result key the server never returns.

The `task` argument and the orchestration resource both said an empty answer "returns
`ok: true`". No tool result has an `ok` key -- only transcripts carry one -- so a caller
checking it found nothing, and the text it was meant to act on pointed at a field that
does not exist. The key to read is `empty_response`.

The check is general rather than a grep for `ok`: every backticked `key: value` in a tool
description, an argument description or the orchestration resource must name a key some
tool's `outputSchema` declares, at any depth. A rename that strands a sentence fails here.
"""

from __future__ import annotations

import asyncio
import re

from fastmcp import Client
from test_server import _built

KEY_VALUE = re.compile(r"`([a-z_][a-z0-9_]*): [^`]+`")


def _declared_keys(schema: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(schema, dict):
        for name, sub in (schema.get("properties") or {}).items():
            keys.add(name)
            keys |= _declared_keys(sub)
        for part in ("items", "additionalProperties"):
            keys |= _declared_keys(schema.get(part))
        for part in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(part) or []:
                keys |= _declared_keys(sub)
        for sub in (schema.get("$defs") or {}).values():
            keys |= _declared_keys(sub)
    return keys


def _texts_and_keys():
    async def go():
        async with Client(_built()) as client:
            tools = await client.list_tools()
            resource = await client.read_resource("delegate://orchestration")
            return tools, "".join(getattr(c, "text", "") for c in resource)

    tools, orchestration = asyncio.run(go())
    texts = {"delegate://orchestration": orchestration}
    declared: set[str] = set()
    for tool in tools:
        texts[tool.name] = tool.description or ""
        for arg, spec in (tool.inputSchema.get("properties") or {}).items():
            texts[f"{tool.name}.{arg}"] = spec.get("description") or ""
        declared |= _declared_keys(tool.outputSchema or {})
    return texts, declared


def _stranded(texts: dict[str, str], declared: set[str]) -> list[str]:
    return sorted(
        f"{where}: `{key}`"
        for where, text in texts.items()
        for key in KEY_VALUE.findall(" ".join(text.split()))
        if key not in declared
    )


def test_every_result_key_a_description_names_is_one_the_server_returns():
    texts, declared = _texts_and_keys()
    assert not _stranded(texts, declared)


def test_the_check_fires_on_a_key_nothing_returns():
    """Negative control: the same scan over a planted sentence must object."""
    texts, declared = _texts_and_keys()
    texts["planted"] = "an empty answer returns `ok: true`"
    assert _stranded(texts, declared) == ["planted: `ok`"]
