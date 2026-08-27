"""The MCP wiring, driven through a transport double and a real MCP client.

`backend_status()` exists to answer one question a stack trace cannot: is the model I
was told to use actually there. Every status word it can report has a test, because the
caller acts differently on each -- `auth_failed` is a .env edit, `backend_unreachable`
is someone else's hardware -- and a vocabulary nothing distinguishes is not a
vocabulary. Each negative test asserts the check *fires*.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastmcp import Client

from claude_delegate_local import server
from claude_delegate_local.backends import openai_compat as oc
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry, Registry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


def registry(*entries: ModelEntry, default: str = "flash") -> Registry:
    return Registry(entries={e.key: e for e in entries}, default_key=default)


def models_reply(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


class DoubleCache(server.BackendCache):
    """A cache whose backends speak to a transport double instead of a socket."""

    def __init__(self, config: Config, handler) -> None:
        super().__init__(config)
        self._handler = handler
        self.built = 0

    def get(self, model: ModelEntry):
        backend = self._backends.get(model.key)
        if backend is None:
            self.built += 1
            client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
            backend = oc.OpenAICompatBackend(self._cfg, model, client=client)
            self._backends[model.key] = backend
        return backend


def probe(model: ModelEntry, handler, *, config: Config | None = None) -> dict:
    """Run one probe against a transport double and return its row."""
    config = config or cfg()
    cache = DoubleCache(config, handler)

    async def go():
        try:
            return await server.probe_entry(cache, config, model, is_default=True)
        finally:
            await cache.aclose()

    return asyncio.run(go())


def ok_handler(*ids: str):
    ids = ids or ("served-id-1",)
    return lambda request: httpx.Response(200, json=models_reply(*ids))


# --- a healthy endpoint ---------------------------------------------------------------


def test_a_healthy_endpoint_serving_the_configured_id_reports_ok_and_confirms_it():
    row = probe(entry(), ok_handler("served-id-1", "another-model"))
    assert row["status"] == server.STATUS_OK
    assert row["id_confirmed"] is True
    assert row["detail"] == ""


def test_the_row_carries_the_registry_facts_the_caller_needs_to_act():
    row = probe(entry(context_window=4096, concurrency=2), ok_handler())
    assert row["key"] == "flash"
    assert row["is_default"] is True
    assert row["api_format"] == "openai"
    assert row["served_model_id"] == "served-id-1"
    assert row["context_window"] == 4096
    assert row["concurrency"] == 2


# --- the mismatch this tool exists to catch -------------------------------------------


def test_an_endpoint_serving_a_different_model_is_reachable_but_not_confirmed():
    """The silent failure. The cluster is up, answers every probe, and is serving
    something other than what the registry names -- so a delegation either gets refused
    or, worse, gets answered by a model nobody chose. Nothing else reports this."""
    row = probe(entry(), ok_handler("some-entirely-other-model"))
    assert row["id_confirmed"] is False
    assert row["status"] == server.STATUS_OK, "a healthy endpoint must not read as down"
    assert "does not serve" in row["detail"]


def test_a_mismatch_is_decided_by_exact_equality_not_by_prefix():
    """Registry.resolve() matches served_model_id exactly and docs/MODELS.md says
    'exactly as the server reports it'. A version suffix is a different model, and
    guessing otherwise would confirm an id the endpoint will refuse."""
    row = probe(entry(served_model_id="served-id-1"), ok_handler("served-id-1-v2"))
    assert row["id_confirmed"] is False


def test_the_confirmed_flag_is_not_hardwired_either_way():
    """The negative control for the two tests above: the same code path must produce
    both answers, or neither assertion proves anything."""
    assert probe(entry(), ok_handler("served-id-1"))["id_confirmed"] is True
    assert probe(entry(), ok_handler("other"))["id_confirmed"] is False


def test_the_row_does_not_list_what_the_endpoint_serves():
    """A model list is somewhere a stray internal name leaks. The count is enough to
    tell 'serving the wrong thing' from 'serving nothing at all'."""
    row = probe(entry(), ok_handler("secret-internal-name-a", "secret-internal-name-b"))
    assert "secret-internal-name-a" not in json.dumps(row)


# --- the failure vocabulary -----------------------------------------------------------


def test_a_dropped_route_is_reported_as_backend_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection failed")

    row = probe(entry(), handler)
    assert row["status"] == server.STATUS_UNREACHABLE
    assert row["id_confirmed"] is None, "an unreachable endpoint can neither confirm nor deny"


def test_a_401_is_reported_as_auth_failed_rather_than_unreachable():
    """A wrong key and a dead cluster are the same HTTP round trip and completely
    different problems: one is a .env edit, the other is someone else's hardware."""
    row = probe(entry(), lambda request: httpx.Response(401, json={"error": "no"}))
    assert row["status"] == server.STATUS_AUTH_FAILED


def test_a_403_is_also_auth_failed():
    row = probe(entry(), lambda request: httpx.Response(403, json={"error": "no"}))
    assert row["status"] == server.STATUS_AUTH_FAILED


def test_a_500_is_refused_rather_than_auth_failed():
    """The negative control for the two above: if every non-2xx mapped to auth_failed,
    those tests would pass and the distinction would be fictional."""
    row = probe(entry(), lambda request: httpx.Response(500, text="boom"))
    assert row["status"] == server.STATUS_REFUSED


def test_a_2xx_that_is_not_a_model_list_is_a_protocol_error_not_a_refusal():
    """Distinct from backend_refused on purpose: the endpoint is alive and answering,
    it is just not the stack we think it is -- a proxy, or a different API."""
    row = probe(entry(), lambda request: httpx.Response(200, json={"nope": []}))
    assert row["status"] == server.STATUS_PROTOCOL_ERROR
    assert row["status"] != server.STATUS_REFUSED


def test_an_unset_api_key_variable_is_misconfigured_and_never_reaches_the_network():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=models_reply("served-id-1"))

    row = probe(entry(api_key_env="DEFINITELY_UNSET_KEY_VAR"), handler)
    assert row["status"] == server.STATUS_MISCONFIGURED
    assert calls == [], "a settings problem must be caught before a request is attempted"


def test_every_status_word_is_distinguishable_from_every_other():
    """A vocabulary whose words collide cannot be acted on differently, and each test
    above would still pass if two of them were the same string."""
    words = [
        server.STATUS_OK,
        server.STATUS_UNREACHABLE,
        server.STATUS_AUTH_FAILED,
        server.STATUS_REFUSED,
        server.STATUS_PROTOCOL_ERROR,
        server.STATUS_MISCONFIGURED,
    ]
    assert len(set(words)) == len(words)


def test_backend_unreachable_is_spelled_the_way_troubleshooting_promises():
    """docs/TROUBLESHOOTING.md indexes this symptom by name. Renaming the constant
    without renaming the document makes the document a lie."""
    assert server.STATUS_UNREACHABLE == "backend_unreachable"


# --- the endpoint is never named (ADR-0029) -------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        ok_handler("served-id-1"),
        ok_handler("a-different-model"),
        lambda request: httpx.Response(401, json={"error": "denied"}),
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(200, json={"nope": []}),
    ],
    ids=["ok", "mismatch", "auth", "refused", "protocol"],
)
def test_no_status_row_ever_names_the_endpoint(handler):
    """ADR-0029. This result is exactly what someone pastes into a bug report, so the
    address must not be in it -- in any outcome, not just the happy one."""
    row = probe(entry(), handler)
    blob = json.dumps(row)
    assert HOST not in blob
    assert "example.com" not in blob
    assert "8000" not in blob


def test_the_leak_check_would_notice_an_address_that_was_present():
    """The negative test for the check above. Asserting a substring is absent passes
    trivially against an empty string, so prove the assertion can fail at all."""
    blob = json.dumps({"detail": f"could not reach {HOST}"})
    assert "example.com" in blob


# --- one dead entry must not take the report down -------------------------------------


def test_a_dead_entry_does_not_stop_the_healthy_ones_being_reported():
    def handler(request):
        if "example.com" in str(request.url):
            raise httpx.ConnectError("connection failed")
        return httpx.Response(200, json=models_reply("served-id-2"))

    config = cfg()
    cache = DoubleCache(config, handler)
    dead = entry(key="dead")
    live = entry(key="live", base_url="http://example.org:8000", served_model_id="served-id-2")

    async def go():
        try:
            return await asyncio.gather(
                server.probe_entry(cache, config, dead, is_default=True),
                server.probe_entry(cache, config, live, is_default=False),
            )
        finally:
            await cache.aclose()

    rows = asyncio.run(go())
    assert rows[0]["status"] == server.STATUS_UNREACHABLE
    assert rows[1]["status"] == server.STATUS_OK
    assert rows[1]["id_confirmed"] is True


def test_probing_never_raises_whatever_the_endpoint_does():
    """probe_entry() promises to return a finding rather than raise, and the gather in
    the tool relies on that instead of return_exceptions=True."""

    def handler(request):
        raise httpx.ConnectError("connection failed")

    row = probe(entry(), handler)
    assert row["status"] == server.STATUS_UNREACHABLE


# --- the backend cache ----------------------------------------------------------------


def test_two_probes_of_one_entry_share_a_single_backend():
    """The pool is the point. Rebuilding per call throws away connection warmup on
    every delegation, and would open a second pool beside delegate()'s."""
    config = cfg()
    cache = DoubleCache(config, ok_handler())

    async def go():
        try:
            await server.probe_entry(cache, config, entry(), is_default=True)
            await server.probe_entry(cache, config, entry(), is_default=True)
        finally:
            await cache.aclose()

    asyncio.run(go())
    assert cache.built == 1


def test_distinct_entries_do_not_share_a_backend():
    """The negative control: a cache keyed too coarsely would send one model's requests
    to another model's endpoint, and the test above would still pass."""
    config = cfg()
    cache = DoubleCache(config, ok_handler())

    async def go():
        try:
            await server.probe_entry(cache, config, entry(key="a"), is_default=True)
            await server.probe_entry(cache, config, entry(key="b"), is_default=False)
        finally:
            await cache.aclose()

    asyncio.run(go())
    assert cache.built == 2


def test_closing_the_cache_twice_is_harmless():
    """Teardown can run on a path that already tore down; aclose() is documented safe
    to call more than once and this holds us to it."""
    config = cfg()
    cache = DoubleCache(config, ok_handler())

    async def go():
        await server.probe_entry(cache, config, entry(), is_default=True)
        await cache.aclose()
        await cache.aclose()

    asyncio.run(go())


# --- the tool as the client sees it ---------------------------------------------------


async def test_the_server_declares_backend_status_over_a_real_mcp_session():
    """Registration is not obvious from the decorator alone: a tool that fails to
    declare is invisible to Claude Code and the failure is silent."""
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler()))
    async with Client(mcp) as client:
        names = [t.name for t in await client.list_tools()]
    assert "backend_status" in names


async def test_the_declared_description_tells_the_model_what_id_confirmed_means():
    """The description is the model-facing contract. id_confirmed is useless if the
    caller is not told that false-with-ok is the case worth reading."""
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler()))
    async with Client(mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "backend_status")
    assert "id_confirmed" in (tool.description or "")


async def test_the_result_reports_every_registered_entry_and_marks_the_default():
    config = cfg()
    mcp = server.build(
        config,
        registry(entry(key="a"), entry(key="b"), default="b"),
        DoubleCache(config, ok_handler()),
    )
    async with Client(mcp) as client:
        result = (await client.call_tool("backend_status")).data
    assert result["default"] == "b"
    assert {row["key"] for row in result["models"]} == {"a", "b"}
    assert [row["key"] for row in result["models"] if row["is_default"]] == ["b"]
