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
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from claude_delegate_local import loop as loop_module
from claude_delegate_local import sandbox, server
from claude_delegate_local import tools as tools_module
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


# --- delegate(), as the client sees it -------------------------------------------------


def chat_reply(content="ok", finish_reason="stop", model="served-id-1", **over):
    body = {
        "model": model,
        "choices": [
            {"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    body.update(over)
    return body


def chat_handler(**over):
    return lambda request: httpx.Response(200, json=chat_reply(**over))


def delegated(handler, *, entries=None, config=None, **kwargs):
    """Call delegate() over a real MCP session against a transport double."""
    config = config or cfg()
    entries = entries or (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    # `effort` is required on every delegation tool, and "inherit" is the value that
    # preserves the pre-ADR-0045 behaviour exactly: it falls through to the agent, the
    # registry row and then `thinking_default`. A test that cares about the level sets
    # it itself; that it is *refused* when absent is asserted directly, not here.
    kwargs.setdefault("effort", "inherit")

    async def go():
        async with Client(mcp) as client:
            return (await client.call_tool("delegate", kwargs)).data

    return asyncio.run(go())


def test_exactly_seven_tools_are_declared():
    """docs/AGENTS.md promises this exact set, and this is what holds it to that.

    The promise is the design: a new *kind* of delegated task is a markdown file, not
    another tool. Asserting the exact set rather than membership is deliberate -- one
    added without argument would otherwise pass, and the cost of that is paid by every
    caller whose tool list grows, not by whoever added it.

    It said five until `delegate_readonly`, which is the argument ADR-0005 asked this test
    to force rather than a way around it: a read-only call cannot be expressed where
    permission rules match on tool name and never inspect arguments, so the constraint has
    to be a tool. Changing this number is the moment to write that argument down, and
    ADR-0042 is where it went.

    It says seven for the second application of that same argument: a batch could be
    narrowed to read-only by an argument, and an argument is exactly what the permission
    layer cannot see. The number will not go up again for a *task* -- that is still a
    markdown file -- only for a promise a caller must be able to make before the call.
    """
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler()))

    async def go():
        async with Client(mcp) as client:
            return [t.name for t in await client.list_tools()]

    assert set(asyncio.run(go())) == {
        "delegate", "delegate_readonly", "delegate_to_agent", "delegate_batch",
        "delegate_batch_readonly", "list_agents", "backend_status",
    }


def test_delegate_returns_the_answer_and_the_bookkeeping():
    result = delegated(chat_handler(content="the answer"), task="a question")
    assert result["answer"] == "the answer"
    assert result["finish_reason"] == "stop"
    assert result["input_tokens"] == 7
    assert result["output_tokens"] == 3
    assert result["empty_response"] is False


def test_the_reported_model_is_what_the_backend_said_not_what_was_asked_for():
    """ADR-0007. Echoing the argument back would report success at using a model that
    may never have served the request."""
    result = delegated(chat_handler(model="something-else-entirely"), task="x")
    assert result["model"] == "something-else-entirely"


def test_the_reported_effort_is_the_one_actually_resolved():
    result = delegated(chat_handler(), entries=(entry(default_effort="max"),), task="x")
    assert result["effort"] == "max"


# --- the empty answer that is not an answer -------------------------------------------


def scripted_handler(replies):
    """Answer each call with the next item, so a test can span the recovery stages.

    An endpoint that returns the same thing forever cannot distinguish "tried the
    mitigations and they did not help" from "never tried them", and those are the two
    states these tests exist to keep apart.
    """
    remaining = list(replies)

    def handle(request):
        item = remaining.pop(0) if remaining else replies[-1]
        return httpx.Response(200, json=chat_reply(**item))

    return handle


EXHAUSTED = {"content": None, "finish_reason": "length"}


def test_an_empty_answer_at_a_length_stop_is_flagged_rather_than_returned_bare():
    """The reply budget went entirely on reasoning and left nothing to answer with.
    Returning {"answer": ""} alone reads as a model with nothing to say, which is a
    different thing and leads the caller to report a false result. ADR-0014."""
    result = delegated(chat_handler(content=None, finish_reason="length"), task="x")
    assert result["answer"] == ""
    assert result["empty_response"] is True
    assert result["finish_reason"] == "length", "the raw reason must survive uninterpreted"


def test_an_ordinary_answer_is_not_flagged_as_empty():
    """The negative control. A hardwired True would satisfy the test above and mark
    every successful delegation as a failure."""
    result = delegated(chat_handler(content="real content"), task="x")
    assert result["empty_response"] is False


def test_the_server_recovers_an_empty_answer_before_reporting_one():
    """The endpoint answers on the second call, so the mitigation is what produced the
    answer. Without this the caller would have been handed "" and told to retry -- which
    was the old contract, and cost cloud tokens to do what the server can do here."""
    handler = scripted_handler([EXHAUSTED, {"content": "recovered on the retry"}])
    result = delegated(handler, task="x")
    assert result["answer"] == "recovered on the retry"
    assert result["empty_response"] is False
    assert result["reasoning_exhausted"] is False
    assert result["attempts"] == 2, "the count is real calls, not an estimate"


def test_reasoning_exhausted_is_only_claimed_after_the_mitigations_were_really_spent():
    """TROUBLESHOOTING defines reasoning_exhausted_budget as the state after every
    mitigation was tried, so the claim has to be backed by attempts that happened. Here
    the endpoint returns the signature every time, and exhaustion is the honest verdict."""
    result = delegated(scripted_handler([EXHAUSTED]), task="x", effort="max")
    assert result["empty_response"] is True
    assert result["reasoning_exhausted"] is True
    assert result["attempts"] > 1, "the verdict is worthless if nothing was tried"


def test_reasoning_exhausted_is_false_when_there_was_no_effort_left_to_step_down():
    """The wrong-diagnosis case, at the tool surface rather than inside loop.py. With the
    effort already off, an empty answer means the budget was too small -- not reasoning
    that would not fit. The two send a caller to opposite fixes, which is the whole reason
    this is a second field rather than a wording change to the first."""
    result = delegated(scripted_handler([EXHAUSTED]), task="x", effort="off")
    assert result["empty_response"] is True
    assert result["reasoning_exhausted"] is False


def test_a_successful_answer_is_never_marked_exhausted():
    """The negative control for the two above: a hardwired True would satisfy them and
    condemn every healthy delegation."""
    result = delegated(chat_handler(content="fine"), task="x")
    assert result["reasoning_exhausted"] is False


# --- delegate() error mapping ---------------------------------------------------------


def refusal(handler, **kwargs) -> str:
    from fastmcp.exceptions import ToolError

    try:
        delegated(handler, **kwargs)
    except ToolError as e:
        return str(e)
    raise AssertionError("expected the call to be refused")


def test_an_unreachable_backend_is_refused_with_the_word_troubleshooting_indexes():
    def handler(request):
        raise httpx.ConnectError("connection failed")

    assert server.STATUS_UNREACHABLE in refusal(handler, task="x")


def test_a_401_is_refused_as_auth_failed():
    assert server.STATUS_AUTH_FAILED in refusal(
        lambda request: httpx.Response(401, text="no"), task="x"
    )


def test_a_500_is_refused_as_backend_refused_carrying_the_status():
    message = refusal(lambda request: httpx.Response(500, text="boom"), task="x")
    assert server.STATUS_REFUSED in message
    assert "500" in message


def test_a_protocol_error_is_distinguishable_from_a_refusal():
    """Two different problems: one is the endpoint saying no, the other is the endpoint
    not being what we think it is. Identical text would make the distinction fiction."""
    protocol = refusal(lambda request: httpx.Response(200, json={"nope": 1}), task="x")
    refused = refusal(lambda request: httpx.Response(500, text="boom"), task="x")
    assert server.STATUS_PROTOCOL_ERROR in protocol
    assert protocol != refused


def test_an_unknown_model_is_refused_naming_the_registered_ones():
    message = refusal(chat_handler(), task="x", model="no-such-model")
    assert "flash" in message


def test_an_unlisted_effort_is_refused_by_the_tool():
    assert "effort" in refusal(chat_handler(), task="x", effort="medium")


def test_no_refusal_from_delegate_ever_names_the_endpoint():
    """ADR-0029 again, on the other tool. An error string reaches a log and from there
    a pasted issue comment."""
    def dropped(request):
        raise httpx.ConnectError("connection failed")

    for handler in (
        dropped,
        lambda request: httpx.Response(401, text="denied"),
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(200, json={"nope": 1}),
    ):
        message = refusal(handler, task="x")
        assert "example.com" not in message
        assert "8000" not in message


# --- files[] prefetch, as the client sees it -------------------------------------------

# paths.py resolves and checks real POSIX paths, so these need the filesystem the server
# actually runs on. Skipped elsewhere with a reason, rather than quietly inflating a pass.
FILES_UNPROVEN = (
    "files[] PREFETCH UNPROVEN BY THIS RUN -- this is not a pass. It needs a POSIX "
    "filesystem; the server runs in WSL. See CONTRIBUTING.md for the invocation."
)
files_posix_only = pytest.mark.skipif(os.name != "posix", reason=FILES_UNPROVEN)


def files_cfg(tmp_path, **over) -> Config:
    globs = tmp_path / "globs.txt"
    globs.write_text(".env\n*secret*\n", encoding="utf-8")
    kw = {
        "workspace_roots": (os.path.realpath(tmp_path),),
        "secret_globs_file": str(globs),
        "respect_gitignore": False,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def recording_handler(sent: list):
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply())

    return handler


@files_posix_only
def test_a_named_file_reaches_the_model_without_the_caller_pasting_it(tmp_path):
    """The whole point of the tool: the bytes go to the model, never through Claude."""
    target = tmp_path / "refund.py"
    target.write_text("def refund():\n    return 'SENTINEL'\n", encoding="utf-8")

    sent: list = []
    result = delegated(
        recording_handler(sent),
        config=files_cfg(tmp_path),
        task="review this",
        files=[str(target)],
    )

    body = json.dumps(sent[0])
    assert "SENTINEL" in body, "the file contents never reached the request"
    assert result["files_read"][0]["path"] == os.path.realpath(target)


@files_posix_only
def test_the_task_still_comes_last_in_the_message(tmp_path):
    """ADR-0011's ordering, asserted where it is actually assembled."""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    sent: list = []
    delegated(
        recording_handler(sent),
        config=files_cfg(tmp_path),
        task="THE-TASK-TEXT",
        files=[str(target)],
    )
    content = sent[0]["messages"][-1]["content"]
    assert content.index("x = 1") < content.index("THE-TASK-TEXT")


@files_posix_only
def test_a_refused_path_fails_the_call_and_nothing_is_dispatched(tmp_path):
    """A refusal must cost nothing.

    Dispatching first and refusing after would burn a delegation on a call that was never
    going to be allowed -- and would make the refusal depend on the cluster being up.
    """
    outside = tmp_path.parent / "elsewhere.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    sent: list = []
    with pytest.raises(Exception) as e:  # fastmcp re-raises its own error type
        delegated(
            recording_handler(sent),
            config=files_cfg(tmp_path),
            task="review this",
            files=[str(outside)],
        )

    assert sent == [], "the backend was called despite a refused path"
    assert "workspace root" in str(e.value)


@files_posix_only
def test_every_refused_path_is_named_in_one_error(tmp_path):
    """One round trip has to be enough to fix all of them."""
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    missing = tmp_path / "gone.py"

    sent: list = []
    with pytest.raises(Exception) as e:
        delegated(
            recording_handler(sent),
            config=files_cfg(tmp_path),
            task="review this",
            files=[str(secret), str(missing)],
        )

    message = str(e.value)
    assert "client_secret.json" in message
    assert "gone.py" in message
    assert sent == []


@files_posix_only
def test_a_skipped_file_lets_the_call_proceed_and_is_reported(tmp_path):
    """The other half of the refusal/skip split: a skip is not a failure."""
    good = tmp_path / "a.py"
    good.write_text("x = 1\n", encoding="utf-8")
    blob = tmp_path / "b.py"
    blob.write_bytes(b"\x00\x01binary")

    result = delegated(
        chat_handler(),
        config=files_cfg(tmp_path),
        task="review this",
        files=[str(good), str(blob)],
    )

    assert result["answer"] == "ok"
    assert [f["path"] for f in result["files_read"]] == [os.path.realpath(good)]
    assert result["files_skipped"][0]["kind"] == "binary"


def test_the_tool_description_tells_the_model_not_to_paste_files():
    """The description is the model-facing contract, and this is the behaviour it buys.

    A model that reads a file itself in order to paste it into `task` has spent exactly
    the context this tool exists to save, and the call still succeeds -- so nothing but
    the wording prevents it.
    """
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler()))

    async def go():
        async with Client(mcp) as client:
            return next(t for t in await client.list_tools() if t.name == "delegate")

    tool = asyncio.run(go())
    # Collapsed, because the docstring is hard-wrapped and a phrase can straddle a line
    # break. The contract is the words, not where they happen to sit.
    description = " ".join((tool.description or "").split())
    assert "files[]" in description
    assert "never enter your context" in description
    assert "files_skipped" in description


# --- the agentic loop, over a real MCP session ---------------------------------------------


def tool_call_reply(name: str, args: dict, *, call_id: str = "call-0"):
    """A chat-completions reply that asks for a tool, in the shape the adapter reads."""
    return {
        "id": "cmpl-1",
        "model": "served-id-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def turn_handler(*replies):
    """Serves one canned HTTP reply per request, so a delegation can span real turns."""
    remaining = list(replies)

    def handler(request):
        return httpx.Response(200, json=remaining.pop(0))

    return handler


def test_delegate_does_not_expose_the_injected_context_to_the_model():
    """`ctx` is wiring. A model that saw it in the schema could try to fill it in."""
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler()))

    async def go():
        async with Client(mcp) as client:
            return next(t for t in await client.list_tools() if t.name == "delegate")

    schema = asyncio.run(go()).inputSchema
    assert "ctx" not in schema["properties"]
    assert "allowed_tools" in schema["properties"]


@files_posix_only
def test_a_delegation_that_calls_a_tool_runs_more_than_one_turn(tmp_path):
    """The whole point of M4, proved through the MCP surface rather than at the loop."""
    target = tmp_path / "note.py"
    target.write_text("# hello\n", encoding="utf-8")
    config = cfg(workspace_roots=(str(tmp_path),))
    result = delegated(
        turn_handler(
            tool_call_reply("read_file", {"path": str(target)}),
            chat_reply(content="it says hello"),
        ),
        config=config,
        task="what does the file say",
    )
    assert result["answer"] == "it says hello"
    assert result["turns"] == 2
    assert result["tool_calls"] == 1
    assert result["tool_calls_by_name"] == {"read_file": 1}
    assert result["tool_errors"] == 0
    assert result["hit_turn_limit"] is False
    # Present at zero once a loop ran, sharing the gate with the counters above rather than
    # getting a narrower one. None, not 0: nothing exited, and 0 is a real exit code.
    assert result["bash_calls"] == 0
    assert result["bash_failures"] == 0
    assert result["last_bash_exit"] is None


def test_an_empty_toolset_takes_the_one_shot_path_and_reports_no_ledger():
    """`tool_calls: 0` next to an answer would read as a model that chose not to use its
    tools. It was never offered any, and the caller acts on that differently."""
    result = delegated(
        chat_handler(content="the answer"), task="a question", allowed_tools=[]
    )
    assert result["answer"] == "the answer"
    assert "turns" not in result
    assert "tool_calls" not in result
    # And the breakdown with it. An empty map beside an answer says the same wrong thing the
    # zero would: that the model was offered tools and declined them.
    assert "tool_calls_by_name" not in result
    # The bash counters share that gate rather than getting one of their own.
    assert "bash_calls" not in result
    assert "last_bash_exit" not in result


@files_posix_only
def test_the_ledger_says_which_tools_were_called_not_only_how_many(tmp_path):
    """The distinction the total cannot carry. `tool_calls: 3` is the same number whether a
    delegation read three files or overwrote three, and `transcript_dir` is empty by default
    -- so without this the only surviving account of which it was is the model's own prose,
    which is exactly what the ledger exists not to trust (ADR-0007).

    Asserted as a sum as well as a map: the two are rendered from one counter and must not be
    able to drift, because a breakdown that disagreed with the total would be worse than no
    breakdown at all.
    """
    one = tmp_path / "one.py"
    one.write_text("# one\n", encoding="utf-8")
    two = tmp_path / "two.py"
    two.write_text("# two\n", encoding="utf-8")
    config = cfg(workspace_roots=(str(tmp_path),))

    result = delegated(
        turn_handler(
            tool_call_reply("read_file", {"path": str(one)}, call_id="c0"),
            tool_call_reply("read_file", {"path": str(two)}, call_id="c1"),
            tool_call_reply(
                "write_file",
                {"path": str(tmp_path / "three.py"), "content": "# three\n"},
                call_id="c2",
            ),
            chat_reply(content="done"),
        ),
        config=config,
        task="read both and write a third",
    )

    assert result["tool_calls"] == 3
    assert result["tool_calls_by_name"] == {"read_file": 2, "write_file": 1}
    assert sum(result["tool_calls_by_name"].values()) == result["tool_calls"]


def test_the_default_delegation_offers_every_available_tool(monkeypatch):
    """On a host that can confine a shell. Bubblewrap is stubbed because its presence is a
    property of the machine running the suite, not of the wiring under test, and this test
    should say the same thing on Windows as it does in WSL."""
    monkeypatch.setattr(sandbox, "available", lambda _cfg: True)
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="done"))

    delegated(handler, task="a question")
    declared = {t["function"]["name"] for t in seen[0].get("tools", [])}
    assert declared == {
        "read_file",
        "search_files",
        "write_file",
        "edit_file",
        "run_bash",
    }


def test_a_host_without_bubblewrap_is_not_offered_run_bash(monkeypatch):
    """The same route, on the other kind of host, asserted where the model actually reads it.

    `tests/test_tools.py` proves the resolved set drops the tool; this proves the drop
    survives every layer between there and the wire, which is the part a unit test cannot
    see. Before this, a delegation on such a host spent a turn calling `run_bash` and being
    told no (JOURNAL 2026-08-29).
    """
    monkeypatch.setattr(sandbox, "available", lambda _cfg: False)
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="done"))

    delegated(handler, task="a question")
    declared = {t["function"]["name"] for t in seen[0].get("tools", [])}
    assert declared == {"read_file", "search_files", "write_file", "edit_file"}


def test_progress_is_notified_to_the_client_once_per_turn(tmp_path):
    """ADR-0018, end to end. The client's stdio idle timer is what this resets, so a
    notification the loop emits but the session never sends would be no use at all.

    The tool call is one the path policy refuses, which is deliberate: a refusal is still a
    turn, it costs no filesystem, and what is under test here is the notification rather
    than the tool. Two turns must produce two notifications either way.

    `slots_dir` is isolated for the same reason the batch-concurrency test isolates it:
    the default is the machine's real one. A delegation that waits for a slot emits an
    extra `progress(0, 0)` from `ticked`, so sharing the file with the other pytest
    workers turns an exact-sequence assertion into a coin flip. Seen once under load.
    """
    config = cfg(max_turns_default=4, slots_dir=str(tmp_path))
    mcp = server.build(
        config,
        registry(entry()),
        DoubleCache(
            config,
            turn_handler(
                tool_call_reply("read_file", {"path": "/nowhere/at/all.py"}),
                chat_reply(content="I could not read it"),
            ),
        ),
    )
    seen: list[tuple[float, float | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total))

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            call = client.call_tool(
                "delegate", {"task": "read it", "effort": "inherit"})
            return (await call).data

    result = asyncio.run(go())
    assert result["turns"] == 2
    assert result["tool_errors"] == 1, "the refusal is the turn, and it is reported as one"
    assert [p for p, _ in seen] == [1, 2]
    assert {total for _, total in seen} == {4}, "the budget reported is the turn budget"


def test_a_one_turn_delegation_still_notifies(tmp_path):
    """The other direction is not 'no notification' -- it is that the turn doing all the
    waiting is exactly the one that must not be skipped.

    `slots_dir` is isolated as above: a wait for a slot adds a notification this counts.
    """
    config = cfg(slots_dir=str(tmp_path))
    mcp = server.build(config, registry(entry()), DoubleCache(config, chat_handler(content="x")))
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total))

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            await client.call_tool("delegate", {"task": "q", "effort": "inherit"})

    asyncio.run(go())
    assert len(seen) == 1


def test_a_caller_named_budget_reaches_the_wire():
    """ADR-0024's precedence, through the surface a caller actually uses."""
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="done"))

    delegated(handler, task="a question", max_tokens=4096)
    assert seen[0]["max_tokens"] == 4096


def test_without_one_the_configured_budget_is_used():
    """The other direction: a server that always forwarded 4096 would pass the test above."""
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="done"))

    delegated(handler, config=cfg(max_tokens=1234), task="a question")
    assert seen[0]["max_tokens"] == 1234


BWRAP_REASON = (
    "THE OPENED ROUTE IS UNPROVEN BY THIS RUN -- this is not a pass. It runs a real "
    "command in a real sandbox, which needs bubblewrap: WSL, not Windows. Run: wsl -d "
    "Ubuntu-24.04 -e bash -lc 'cd <repo> && ~/.venvs/delegate/bin/python -m pytest "
    "tests/test_server.py'"
)


def needs_bwrap(fn):
    """Both markers, always, because either alone is not enough.

    `skipif` on a missing bubblewrap is what keeps this quiet on Windows. It is *not* what
    keeps it quiet in CI: the runner installs bubblewrap, so `which` finds one, and then
    every invocation fails anyway because the sandbox cannot bring up loopback in a new
    network namespace without CAP_NET_ADMIN. The `integration` marker is what CI excludes,
    and a test carrying only the skipif runs there and fails.

    Composed into one decorator rather than left as two, because that is exactly the pair
    that got separated once.
    """
    return pytest.mark.integration(
        pytest.mark.skipif(shutil.which("bwrap") is None, reason=BWRAP_REASON)(fn))



@needs_bwrap
def test_a_delegation_reaches_a_real_shell_and_the_ledger_contradicts_the_model():
    """The whole of M5, end to end, and the only test that could not exist before it.

    Every layer below this was reachable while `run_bash` was withheld, because
    `execute_tool` takes its allowed set as a parameter. This one is not: it goes through
    the MCP surface, so the tool has to be declared for the call to be routed at all.

    The model is scripted to claim success after a command that exits 3. That is the exact
    misreport ADR-0007 was written about, and the assertion is that the ledger disagrees
    with the answer sitting beside it -- not that the two agree.
    """
    result = delegated(
        turn_handler(
            tool_call_reply("run_bash", {"command": "exit 3"}),
            chat_reply(content="All checks passed cleanly."),
        ),
        task="run the checks",
    )
    assert result["answer"] == "All checks passed cleanly."
    assert result["bash_calls"] == 1
    assert result["bash_failures"] == 1
    assert result["last_bash_exit"] == 3


@needs_bwrap
def test_the_ledger_agrees_with_the_model_when_the_model_is_right():
    """The other half. Without it, a ledger that reported failure unconditionally would
    pass the test above while being useless."""
    result = delegated(
        turn_handler(
            tool_call_reply("run_bash", {"command": "exit 0"}),
            chat_reply(content="it worked"),
        ),
        task="run the checks",
    )
    assert result["bash_calls"] == 1
    assert result["bash_failures"] == 0
    assert result["last_bash_exit"] == 0


# --- delegate_to_agent, delegate_batch, list_agents ---------------------------------------


def build_default():
    """A server on the default config, for tests that only read the declared tool surface."""
    config = cfg()
    return server.build(config, registry(entry()), DoubleCache(config, ok_handler("served-id-1")))


def called(handler, tool, *, entries=None, config=None, **kwargs):
    """Call any tool over a real MCP session against a transport double."""
    config = config or cfg()
    entries = entries or (entry(),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    # This helper also drives `backend_status`, which has no `effort` and refuses one.
    if tool.startswith("delegate"):
        kwargs.setdefault("effort", "inherit")

    async def go():
        async with Client(mcp) as client:
            return (await client.call_tool(tool, kwargs)).data

    return asyncio.run(go())


def agent_file(root: Path, name: str, frontmatter: str = "", body: str = "You help.") -> Path:
    d = root / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    path.write_text(f"---\nname: {name}\n{frontmatter}---\n{body}\n", encoding="utf-8")
    return path


@files_posix_only
def test_the_agent_body_reaches_the_model_and_the_system_prompt_does_not_move(tmp_path):
    """ADR-0011 at the wire, which is the only place it can actually be checked.

    A unit test can assert the body lands in the user message; only the request that leaves
    the server proves the system prompt was not quietly rebuilt around it on the way.
    """
    agent_file(tmp_path, "helper", body="AGENT INSTRUCTIONS HERE")
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="done"))

    called(handler, "delegate_to_agent",
           config=cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere")),
           agent_name="helper", task="the task", workdir=str(tmp_path))

    messages = seen[0]["messages"]
    system = [m for m in messages if m["role"] == "system"]
    user = [m for m in messages if m["role"] == "user"]
    assert "AGENT INSTRUCTIONS HERE" in user[0]["content"]
    assert all("AGENT INSTRUCTIONS HERE" not in m["content"] for m in system)
    body = user[0]["content"]
    assert body.index("AGENT INSTRUCTIONS HERE") < body.index("the task")


@files_posix_only
def test_the_frontmatter_model_actually_binds(tmp_path):
    """The ancestor bug, asserted where it does damage: on the wire.

    `model:` was loaded and ignored there. Here the agent names the second registry entry,
    and what proves it bound is the served id in the request -- not the parsed spec, which
    would be true even if nothing consumed it.
    """
    agent_file(tmp_path, "big", frontmatter="model: second\n")
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=chat_reply(content="done"))

    called(handler, "delegate_to_agent",
           entries=(entry(), entry(key="second", served_model_id="served-id-2")),
           config=cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere")),
           agent_name="big", task="t", workdir=str(tmp_path))

    assert seen[0] == "served-id-2", "the frontmatter model did not reach the backend"


@files_posix_only
def test_an_explicit_model_still_beats_the_agent_file(tmp_path):
    """Precedence, in the direction that lets one hard case go to a larger model without
    editing a file everyone else uses."""
    agent_file(tmp_path, "big", frontmatter="model: second\n")
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=chat_reply(content="done"))

    called(handler, "delegate_to_agent",
           entries=(entry(), entry(key="second", served_model_id="served-id-2")),
           config=cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere")),
           agent_name="big", task="t", model="flash", workdir=str(tmp_path))

    assert seen[0] == "served-id-1"


@files_posix_only
def test_the_result_names_the_file_that_shaped_it(tmp_path):
    """The lookup has three tiers, so the name alone does not identify what was read."""
    path = agent_file(tmp_path, "helper")
    result = called(chat_handler(content="a"), "delegate_to_agent",
                    config=cfg(workspace_roots=(str(tmp_path),),
                               agents_dir=str(tmp_path / "nowhere")),
                    agent_name="helper", task="t", workdir=str(tmp_path))
    assert result["agent"] == "helper"
    assert result["agent_source"] == str(path)


@files_posix_only
def test_a_missing_agent_is_refused_before_anything_is_sent(tmp_path):
    def explode(request):
        raise AssertionError("the backend was called for an agent that does not exist")

    with pytest.raises(Exception, match="No agent named"):
        called(explode, "delegate_to_agent",
               config=cfg(workspace_roots=(str(tmp_path),),
                          agents_dir=str(tmp_path / "nowhere")),
               agent_name="ghost", task="t", workdir=str(tmp_path))


@files_posix_only
def test_a_workdir_outside_every_root_is_refused_before_anything_is_sent(tmp_path):
    """The path check costs nothing and must not depend on the cluster being reachable."""
    agent_file(tmp_path, "helper")

    def explode(request):
        raise AssertionError("the backend was called with an unchecked workdir")

    with pytest.raises(Exception, match="outside every workdir root"):
        called(explode, "delegate_to_agent",
               config=cfg(workspace_roots=(str(tmp_path),),
                          agents_dir=str(tmp_path / "nowhere")),
               agent_name="helper", task="t", workdir=str(tmp_path.parent))


# --- delegate_batch -----------------------------------------------------------------------

# Both batch tools, wherever the assertion is about the batch rather than about its tool
# set. They share one body (`_run_batch`), and a shared body is exactly the thing that can
# be changed for one caller while silently changing it for the other -- so the guards, the
# ordering and the per-item `ok`/`error` contract are asserted on both rather than on the
# one that happened to be written first.
BATCH_TOOLS = ("delegate_batch", "delegate_batch_readonly")


@pytest.mark.parametrize("tool", BATCH_TOOLS)
def test_a_batch_returns_one_result_per_task_in_order(tool):
    results = called(chat_handler(content="a"), tool,
                     tasks=["one", "two", "three"])
    assert results["count"] == 3
    assert [r["index"] for r in results["results"]] == [0, 1, 2]
    assert [r["task"] for r in results["results"]] == ["one", "two", "three"]
    assert results["failed"] == []
    assert all(r["ok"] for r in results["results"])


@files_posix_only
def test_a_batch_item_carries_the_per_tool_breakdown_without_diagnostics(tmp_path):
    """Where the breakdown is worth most, and the one place the old answer was unreachable.

    `diagnostics` already held per-turn `(name, outcome)` pairs, but both batch tools
    hard-code it off -- deliberately, since interleaved per-turn detail from items running at
    once describes nothing. So a batch item could report `tool_calls: 3` and nothing else,
    which is exactly the call whose items a caller is least able to watch.

    One item, not two: `turn_handler` serves canned replies in order, and two concurrent
    items would pair them by whichever request arrived first.

    `slots_dir` is isolated for the reason the concurrency test below gives, and this test
    is why that reason is worth repeating: left at the default it took real slots from the
    machine-wide budget for two turns, and a one-shot test running on another xdist worker
    queued behind it and emitted the admission-wait ticks it asserts it never sends. The
    failure surfaced three tests away from its cause and only under parallel load.
    """
    target = tmp_path / "note.py"
    target.write_text("# hello\n", encoding="utf-8")

    results = called(
        turn_handler(
            tool_call_reply("read_file", {"path": str(target)}),
            chat_reply(content="it says hello"),
        ),
        "delegate_batch",
        config=cfg(workspace_roots=(str(tmp_path),), slots_dir=str(tmp_path / "slots")),
        tasks=["what does the file say"],
    )

    item = results["results"][0]
    assert item["ok"] and item["tool_calls"] == 1
    assert item["tool_calls_by_name"] == {"read_file": 1}
    assert "diagnostics" not in item, "the batch must not have started sending per-turn detail"


@files_posix_only
def test_every_item_in_a_batch_shares_the_prefix_and_differs_only_in_the_task(tmp_path):
    """The reason the tool exists. If the shared part varied per item there would be no
    cache to hit and a batch would be N separate calls wearing one name."""
    agent_file(tmp_path, "helper", body="SHARED AGENT BODY")
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json=chat_reply(content="done"))

    called(handler, "delegate_batch",
           config=cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere")),
           tasks=["alpha", "beta"], agent_name="helper", workdir=str(tmp_path))

    assert len(seen) == 2
    prefixes = {body[: body.rindex("\n\n")] for body in seen}
    assert len(prefixes) == 1, "the shared prefix differed between items"
    assert all("SHARED AGENT BODY" in body for body in seen)
    assert {body[body.rindex("\n\n") + 2 :] for body in seen} == {"alpha", "beta"}


@pytest.mark.parametrize("tool", BATCH_TOOLS)
def test_one_failing_item_does_not_discard_the_others(tool):
    """The contract. Work already paid for is never thrown away because a later item was
    refused -- and on a shared cluster a single transient refusal is not rare."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if json.loads(request.content)["messages"][-1]["content"].endswith("poison"):
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json=chat_reply(content="fine"))

    results = called(handler, tool,
                     config=cfg(retry_max_attempts=1),
                     tasks=["good one", "poison", "good two"])

    assert results["failed"] == [1]
    by_index = {r["index"]: r for r in results["results"]}
    assert by_index[0]["ok"] and by_index[0]["answer"] == "fine"
    assert by_index[2]["ok"] and by_index[2]["answer"] == "fine"
    assert not by_index[1]["ok"]
    assert "answer" not in by_index[1]
    assert by_index[1]["error"]


@pytest.mark.parametrize("tool", BATCH_TOOLS)
def test_a_batch_over_the_cap_is_refused_naming_the_setting(tool):
    with pytest.raises(Exception, match="DELEGATE_MAX_BATCH_SIZE"):
        called(chat_handler(), tool,
               config=cfg(max_batch_size=2), tasks=["a", "b", "c"])


@pytest.mark.parametrize("tool", BATCH_TOOLS)
def test_an_empty_batch_is_refused(tool):
    with pytest.raises(Exception, match="nothing to delegate"):
        called(chat_handler(), tool, tasks=[])


def test_a_batch_never_exceeds_the_endpoints_declared_concurrency(tmp_path):
    """`concurrency` is one of the four rules the admission gate checks (ADR-0012).

    It used to be a semaphore local to this tool, which bounded a batch against itself
    and nothing else -- two batches, or a batch beside a plain `delegate`, could still
    exceed the limit it was reading. The bound now comes from the shared gate, so the
    same assertion covers every path rather than this one.

    `slots_dir` is isolated for the reason the backend_status test gives: the default is
    the machine's real one. Sharing it means this test's batch competes for the endpoint's
    two slots with whatever else on the box is delegating -- and once the suite runs in
    parallel, that includes the other pytest workers. Left at the default it read a peak
    of one on CI and reported the code broken when the truth was that its slots had been
    taken by a sibling process.
    """
    live = {"now": 0, "peak": 0}
    overlapped = asyncio.Event()

    async def handler(request):
        # `await`, not `time.sleep`: a blocking sleep pins the event loop, and every item
        # then runs one at a time whatever the semaphore does -- so the test would report
        # a bound that was really just a stalled loop.
        #
        # And an Event rather than a fixed sleep, because a sleep only *probably* overlaps.
        # Each item waits here until a second one has arrived, which makes the overlap a
        # fact rather than a race the scheduler usually wins. It stopped winning on CI the
        # moment the suite went parallel: with workers competing for two cores, 0.02s
        # elapsed before the next item was scheduled and this read peak == 1.
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        if live["now"] >= 2:
            overlapped.set()
        try:
            # Bounded, so a gate that really does admit one at a time fails on the
            # assertion below rather than hanging the suite. The wait is then abandoned
            # for every later item too, so the failure costs one timeout and not eight.
            await asyncio.wait_for(overlapped.wait(), timeout=2)
        except TimeoutError:
            overlapped.set()
        live["now"] -= 1
        return httpx.Response(200, json=chat_reply(content="done"))

    called(handler, "delegate_batch",
           entries=(entry(concurrency=2),),
           config=cfg(max_batch_size=8, slots_dir=str(tmp_path)),
           tasks=[f"task {i}" for i in range(8)])

    assert live["peak"] <= 2, f"ran {live['peak']} at once against an endpoint declaring 2"
    assert live["peak"] > 1, "ran sequentially; the concurrency the registry declares is unused"


# --- list_agents ---------------------------------------------------------------------------


@files_posix_only
def test_list_agents_reports_what_delegate_to_agent_would_find(tmp_path):
    agent_file(tmp_path, "reviewer",
               frontmatter="description: Reviews a diff.\nmodel: second\neffort: high\n")
    agent_file(tmp_path, "writer")

    listed = called(chat_handler(), "list_agents",
                    entries=(entry(), entry(key="second", served_model_id="served-id-2")),
                    config=cfg(workspace_roots=(str(tmp_path),),
                               agents_dir=str(tmp_path / "nowhere")),
                    workdir=str(tmp_path))

    rows = {a["name"]: a for a in listed["agents"]}
    assert listed["count"] == 2
    assert rows["reviewer"]["description"] == "Reviews a diff."
    assert rows["reviewer"]["model"] == "second"
    assert rows["reviewer"]["effort"] == "high"
    assert rows["reviewer"]["source"].endswith("reviewer.md")
    assert rows["writer"]["model"] == "(the server default)"


@files_posix_only
def test_a_broken_agent_file_is_left_out_rather_than_breaking_the_list(tmp_path):
    """Discovery is not validation. One unparseable file must not make every other agent
    undiscoverable -- but asking for it by name still says exactly what is wrong."""
    agent_file(tmp_path, "good")
    (tmp_path / ".claude" / "agents" / "broken.md").write_text(
        "---\nname: broken\nnonsense: 1\n---\nb\n", encoding="utf-8")

    config = cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere"))
    listed = called(chat_handler(), "list_agents", config=config, workdir=str(tmp_path))
    assert [a["name"] for a in listed["agents"]] == ["good"]

    # Named, not merely missing: a caller cannot ask by name for a name it never saw.
    assert listed["skipped_count"] == 1
    assert listed["skipped"][0]["name"] == "broken"
    assert "unknown frontmatter key" in listed["skipped"][0]["reason"]
    assert listed["skipped"][0]["source"].endswith("broken.md")
    assert listed["other_format_count"] == 0

    with pytest.raises(Exception, match="unknown frontmatter key"):
        called(chat_handler(), "delegate_to_agent", config=config,
               agent_name="broken", task="t", workdir=str(tmp_path))


@files_posix_only
def test_the_three_answers_are_distinguishable_in_one_call(tmp_path):
    """"Not there", "there and broken", and "there but not mine" through the real tool.

    Measured on this repository the day this landed: four of its five agent files are
    Claude Code's, so folding that case into `skipped` would leave the broken list
    permanently non-empty here -- and a list that is never empty is one nobody reads.
    """
    agent_file(tmp_path, "mine")
    d = tmp_path / ".claude" / "agents"
    (d / "broken.md").write_text(
        "---\nname: broken\nnonsense: 1\n---\nb\n", encoding="utf-8")
    (d / "theirs.md").write_text(
        "---\nname: theirs\ntools: Read, Grep\n---\nt\n", encoding="utf-8")

    config = cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere"))
    listed = called(chat_handler(), "list_agents", config=config, workdir=str(tmp_path))

    assert [a["name"] for a in listed["agents"]] == ["mine"]
    assert [s["name"] for s in listed["skipped"]] == ["broken"]
    assert [f["name"] for f in listed["other_format"]] == ["theirs"]
    assert listed["other_format"][0]["keys"] == ["tools"]
    # The property worth protecting: non-empty `skipped` always means something to fix.
    assert listed["skipped_count"] == 1
    assert "absent" not in str(listed), "no name should be reported as missing here"


# --- admission control (ADR-0012) ----------------------------------------------------------

def test_backend_status_reports_the_admission_gate():
    """ADR-0012's reporting half: undersubscription is invisible unless something counts it.

    Peaks that never approach their ceiling are the evidence that the ceiling is too low,
    and there is nowhere else to read them from -- the gate is the only thing that knows.
    """
    result = called(ok_handler("served-id-1"), "backend_status")
    gate = result["admission"]

    assert gate["inflight_seqs"] == 0
    for key in (
        "peak_inflight_seqs",
        "peak_inflight_tokens",
        "peak_inflight_large_prefills",
        "admission_wait_seconds_total",
        "admission_wait_seconds_max",
        "admission_wait_count",
        "admission_timeouts",
    ):
        assert key in gate, f"backend_status lost {key}"


def test_backend_status_says_whether_the_budget_is_machine_wide(tmp_path):
    """ADR-0040's reporting half, and the reason it is reported rather than assumed.

    A gate that has quietly narrowed to a single process looks exactly like a working one
    from the outside -- right up until two editor windows saturate the cluster between
    them. So `active` is answered by whether the shared file can actually be read, and it
    has to survive into a real response rather than existing only in the module that
    computes it.

    `slots_dir` is isolated because the default is the real one. Left at the default this
    reads the machine's live counters, so the gauges below assert that nothing anywhere
    on the box is delegating -- which is false whenever another session is, and the
    failure then describes the machine rather than the code. What is under test is that
    the block is reported and internally consistent, and that holds in a directory of
    this test's own.
    """
    result = called(
        ok_handler("served-id-1"), "backend_status", config=cfg(slots_dir=str(tmp_path))
    )
    shared = result["admission"]["cross_process"]

    assert "active" in shared, "backend_status lost the cross-process block entirely"
    if shared["active"]:
        # Zero at idle, and that is right: a record exists only while a process is
        # actually holding a slot, and is removed again on release rather than lingering.
        assert shared["processes_holding_slots"] == 0
        assert shared["inflight_tokens"] == 0
    else:
        # Windows, where the suite runs but the server never does. Saying why is the
        # whole point: a narrowed scope must never read as a healthy one.
        assert shared["reason"], "an inactive shared budget must say why"


def test_a_single_delegate_is_bounded_by_the_endpoints_concurrency_too():
    """The gap the gate closed. `max_inflight_seqs`' own help text says the endpoint's
    limit and the global budget are "both checked", and until the gate existed that was
    false everywhere except inside `delegate_batch`: a plain `delegate` was never checked
    against `concurrency` at all. Two concurrent `delegate` calls are the case a
    batch-local semaphore could not see."""
    live = {"now": 0, "peak": 0}

    async def handler(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return httpx.Response(200, json=chat_reply(content="done"))

    config = cfg()
    entries = (entry(concurrency=1),)
    mcp = server.build(
        config, registry(*entries, default=entries[0].key), DoubleCache(config, handler)
    )

    async def go():
        async with Client(mcp) as client:
            await asyncio.gather(
                *(client.call_tool("delegate", {"task": f"t{i}", "effort": "inherit"})
                  for i in range(4))
            )

    asyncio.run(go())
    assert live["peak"] == 1, (
        f"ran {live['peak']} at once against an endpoint declaring concurrency=1; "
        "a plain delegate is not being counted against it"
    )


def test_a_delegation_that_fails_still_gives_its_slot_back():
    """A leaked slot is permanent, and the gate has no way to notice or recover.

    Exercised through a real failing dispatch rather than by calling `release` directly:
    the whole risk is a path that never reaches the release, so a test that reaches it
    by hand cannot fail against the bug.
    """
    config = cfg()
    entries = (entry(),)
    mcp = server.build(
        config,
        registry(*entries, default=entries[0].key),
        DoubleCache(config, lambda request: httpx.Response(500, text="nope")),
    )

    async def go():
        async with Client(mcp) as client:
            for _ in range(3):
                with pytest.raises(Exception, match="backend_refused"):
                    await client.call_tool("delegate", {"task": "x", "effort": "inherit"})
            return (await client.call_tool("backend_status", {})).data

    gate = asyncio.run(go())["admission"]
    assert gate["inflight_seqs"] == 0, "a failed dispatch kept its sequence slot"
    assert gate["inflight_tokens"] == 0, "a failed dispatch kept its token reservation"
    assert gate["per_entry"]["flash"]["inflight"] == 0
    # The marks are what the operator tunes from, so a failure must not erase them either.
    assert gate["peak_inflight_seqs"] >= 1


# --- the read-only surface -----------------------------------------------------------------


def test_delegate_readonly_offers_only_tools_that_cannot_write():
    """The annotation is true if nothing the model is offered can write -- not if it is
    offered nothing. That was the same claim when the set was empty; ADR-0048 is what made
    the difference visible.

    Still asserted on the wire rather than on the resolved set, because what the model can
    ask for is what was declared to it, and a set that rendered a writing tool into the
    block would be a read-only tool advertising a write however it was resolved.
    """
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="an answer"))

    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))

    async def go():
        async with Client(mcp) as client:
            return (
                await client.call_tool(
                    "delegate_readonly", {"task": "explain this", "effort": "inherit"})
            ).data

    asyncio.run(go())

    assert sent, "nothing was dispatched"
    declared = {t["function"]["name"] for t in sent[0].get("tools") or ()}
    assert declared == {"read_file", "search_files"}, declared
    # The property, not the list: whatever it was offered, none of it writes. Written this
    # way so adding a read-only tool does not need this test edited, while adding a
    # writing one to the set fails here as well as in tests/test_tools.py.
    for name in declared:
        assert not tools_module.REGISTRY[name].writes, f"{name} can write"


def test_every_item_of_a_read_only_batch_is_offered_only_tools_that_cannot_write():
    """Per item, not per call. The batch fans out into separate dispatches, so a tool set
    fixed at the wrapper is only worth anything if it survives the fan-out to every one."""
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="an answer"))

    results = called(handler, "delegate_batch_readonly", tasks=["one", "two", "three"])

    assert results["count"] == 3
    assert len(sent) == 3, sent
    for body in sent:
        declared = {t["function"]["name"] for t in body.get("tools") or ()}
        assert declared, "an item was dispatched with no tools at all"
        for name in declared:
            assert not tools_module.REGISTRY[name].writes, f"{name} can write"


@files_posix_only
def test_an_agent_file_cannot_widen_a_read_only_batch(tmp_path):
    """The path `delegate_readonly` has no equivalent of, because it takes no agent.

    An agent body is markdown in whatever repository is being delegated over, so its
    frontmatter is adversary-controlled in exactly the case this tool is for -- reviewing
    somebody's code. `run_delegation` reads `agent.allowed_tools` only when the caller
    passed none, and this caller always passes a set; drop that `sorted(...)` argument and
    this test fails while every other read-only test still passes.
    """
    agent_file(tmp_path, "greedy",
               frontmatter="allowed_tools: [read_file, write_file]\n")
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="an answer"))

    called(handler, "delegate_batch_readonly",
           config=cfg(workspace_roots=(str(tmp_path),), agents_dir=str(tmp_path / "nowhere")),
           tasks=["one", "two"], agent_name="greedy", workdir=str(tmp_path))

    assert len(sent) == 2, sent
    for body in sent:
        declared = {t["function"]["name"] for t in body.get("tools") or ()}
        assert "write_file" not in declared, declared
        assert declared == {"read_file", "search_files"}, declared


def test_delegate_readonly_has_no_argument_that_could_widen_it():
    """`allowed_tools` is fixed, not defaulted.

    An annotation a caller could falsify by passing an argument is the check that cannot
    fail: the client gates on the declaration, which is per tool, while the permission
    layer never inspects arguments. So the parameter must not exist at all.
    """
    async def go():
        async with Client(build_default()) as client:
            return {t.name: t for t in await client.list_tools()}

    tools = asyncio.run(go())

    for name in ("delegate_readonly", "delegate_batch_readonly"):
        schema = tools[name].inputSchema
        assert "allowed_tools" not in schema.get("properties", {}), (
            f"{name} must not accept allowed_tools; it would let a caller widen "
            "a tool the client has been told is read-only"
        )


def test_only_the_tools_that_cannot_write_declare_themselves_read_only():
    """The false-claim guard, and the reason this test is worth more than the feature.

    `delegate`, `delegate_to_agent` and `delegate_batch` hand the model `write_file` and
    `run_bash` whenever `allowed_tools` is unset. Marking any of them read-only would let
    a client that gates writes on the annotation run one while believing nothing could be
    written -- the failure mode is silent, and the annotation is trusted precisely because
    it is rarely checked.

    The two lists are the whole surface, so a tool added to neither is caught: the third
    assertion below fails on a name this test has not been told about, which is what stops
    a fifth delegating tool arriving unannotated in either direction.
    """
    async def go():
        async with Client(build_default()) as client:
            return {t.name: t.annotations for t in await client.list_tools()}

    seen = asyncio.run(go())
    cannot_write = ("backend_status", "list_agents", "delegate_readonly",
                    "delegate_batch_readonly")
    can_write = ("delegate", "delegate_to_agent", "delegate_batch")

    for name in cannot_write:
        assert seen[name] is not None and seen[name].readOnlyHint is True, (
            f"{name} cannot write and must say so"
        )
    for name in can_write:
        annotations = seen[name]
        claimed = annotations is not None and annotations.readOnlyHint is True
        assert not claimed, (
            f"{name} can be given write_file and run_bash, so it must never declare "
            "itself read-only"
        )
    assert set(seen) == set(cannot_write) | set(can_write), (
        "a tool was added without deciding which side of the read-only line it sits on"
    )


# --- the one-shot keepalive ---------------------------------------------------------


def slow_chat_handler(seconds: float, **over):
    """A backend that takes its time, so a heartbeat has something to beat through."""
    async def handler(request):
        await asyncio.sleep(seconds)
        return httpx.Response(200, json=chat_reply(**over))
    return handler


def one_shot_progress(config, *, seconds=0.35):
    """Run a one-shot against a slow backend and collect what the client saw.

    `delegate` with an explicitly empty toolset, which is what the one-shot path is now.
    It used to be `delegate_readonly`, until that was given the read-only tools and so the
    turn loop (ADR-0048) -- at which point this helper was quietly measuring the loop's
    per-turn notification instead of the keepalive it exists to test.
    """
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, slow_chat_handler(seconds)))
    seen: list[tuple[float, float | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total))

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit", "allowed_tools": []})

    asyncio.run(go())
    return seen


def test_a_slow_one_shot_keeps_the_client_informed():
    """The gap ADR-0018 left open and `run_one_shot`'s own docstring named.

    A one-shot is a single backend call with no turns, so the per-turn notification has
    nothing to hang on and the call is silent for its whole duration. Measured on
    2026-08-31 as the only remaining shape that can reach the client's stdio idle timeout
    and be abandoned while working perfectly.
    """
    seen = one_shot_progress(cfg(keepalive_interval=1), seconds=2.5)
    assert len(seen) >= 2, (
        f"a one-shot lasting 2.5s past a 1s keepalive sent {len(seen)} notification(s)")


def test_a_one_shot_that_returns_promptly_sends_nothing(tmp_path):
    """The other direction. A notification per fast call is noise on the wire, and a
    test that only asserted 'some progress' would pass on a heartbeat that never stopped.

    `slots_dir` is isolated because this asserts an *absence*, and the keepalive is not the
    only thing that can send `progress(0, 0)`: `ticked` sends the same shape while a
    delegation waits for admission. Left at the default this test takes the machine's real
    slots, so a neighbouring xdist worker holding them made it queue and read its own wait
    as a heartbeat that would not stop. A 30s keepalive cannot fire inside a 0.05s call, so
    every notification it can legitimately see is zero -- but only once nothing can queue.
    """
    config = cfg(keepalive_interval=30, slots_dir=str(tmp_path))
    assert one_shot_progress(config, seconds=0.05) == []


def test_the_heartbeat_stops_when_the_dispatch_does():
    """It runs beside the dispatch rather than inside it, so the risk is a task that
    outlives what it was reporting on.

    The waiting happens inside the event loop deliberately. Sleeping after `asyncio.run`
    returns would prove nothing at all -- the loop is closed by then, so a leaked task
    could not fire even if one had been left behind.
    """
    config = cfg(keepalive_interval=1)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, slow_chat_handler(2.5)))
    seen: list[float] = []

    async def on_progress(progress, total, message):
        seen.append(progress)

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            await client.call_tool("delegate_readonly", {"task": "q", "effort": "inherit"})
            during = len(seen)
            await asyncio.sleep(2.5)
            return during, len(seen)

    during, after = asyncio.run(go())
    assert during, "nothing beat at all, so this cannot detect one that never stops"
    assert after == during, "the heartbeat was still running after the dispatch ended"


def test_the_heartbeat_reaches_the_transcript_as_well_as_the_wire(tmp_path):
    """One mechanism, two readers. A stream silent for the whole of a long one-shot is
    indistinguishable from one whose server was killed, which is how the viewer came to
    call an abandoned delegation live."""
    config = cfg(keepalive_interval=1, transcript_dir=str(tmp_path))
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, slow_chat_handler(2.5)))

    async def go():
        async with Client(mcp) as client:
            await client.call_tool("delegate_readonly", {"task": "q", "effort": "inherit"})

    asyncio.run(go())
    streams = list(tmp_path.glob("*.jsonl"))
    assert len(streams) == 1
    events = [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]
    alive = [e for e in events if e.get("t") == "alive"]
    assert alive, f"no alive event was written; got {[e.get('t') for e in events]}"
    assert alive[0]["of_seconds"] == config.dispatch_timeout, (
        "elapsed without the deadline it is measured against says nothing")
    assert alive[0]["elapsed_seconds"] > 0
    assert events[-1]["t"] == "end", "the heartbeat displaced the end event"


def test_a_failing_heartbeat_does_not_take_the_delegation_with_it():
    """It exists to protect a long delegation from being abandoned. One that instead
    killed one -- because a notification could not be delivered, which is not even
    evidence the client is gone -- would be strictly worse than not having it."""
    config = cfg(keepalive_interval=1)
    mcp = server.build(config, registry(entry()),
                       DoubleCache(config, slow_chat_handler(2.5)))

    async def exploding(progress, total, message):
        raise RuntimeError("the client hung up")

    async def go():
        async with Client(mcp, progress_handler=exploding) as client:
            call = client.call_tool(
                "delegate_readonly", {"task": "q", "effort": "inherit"})
            return (await call).data

    assert asyncio.run(go())["answer"] == "ok"


# --- effort is always stated (ADR-0045) ---------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("delegate", {"task": "q"}),
        ("delegate_readonly", {"task": "q"}),
        ("delegate_to_agent", {"agent_name": "helper", "task": "q"}),
        ("delegate_batch", {"tasks": ["q"]}),
        ("delegate_batch_readonly", {"tasks": ["q"]}),
    ],
)
def test_a_delegation_that_names_no_effort_is_refused(tool, args):
    """The negative test the requirement rests on.

    Making a parameter required is only worth anything if omitting it actually fails, and
    "required" here is a property of a schema fastmcp derives from a signature -- not
    something this repository writes down. So assert the refusal, on every tool that
    delegates, rather than trusting that dropping a default had the effect it looks like
    it has.
    """
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler("served-id-1")))

    async def go():
        async with Client(mcp) as client:
            await client.call_tool(tool, args)

    with pytest.raises(Exception, match="effort"):
        asyncio.run(go())


def _effort_on_the_wire(handler_box, **called_kwargs):
    """Call a tool and return the `reasoning_effort` the backend was actually sent.

    Asserted on the wire, not on the resolved variable: the precedence chain has four tiers
    and the only place all four have finished resolving is the request that leaves.
    """
    def handler(request):
        handler_box.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply(content="ok"))

    called(handler, **called_kwargs)
    return handler_box[0]["reasoning_effort"]


def _agent_in(directory, name: str, frontmatter: str) -> None:
    """An agent file in `agents_dir`, so no workdir is needed. A workdir would have to cross
    the path boundary, which is a different subject and is tested where it belongs."""
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\n{frontmatter}\n---\nYou help.\n", encoding="utf-8"
    )


def test_an_explicit_level_is_what_gets_sent():
    seen: list[dict] = []
    assert _effort_on_the_wire(seen, tool="delegate", task="q", effort="high") == "high"


def test_inherit_reaches_the_configured_default_when_nothing_else_says():
    """`"inherit"` is the whole chain, not a synonym for one tier of it: with no agent and
    no registry row, it must land on `thinking_default` exactly as an absent value used to."""
    seen: list[dict] = []
    got = _effort_on_the_wire(seen, tool="delegate", task="q", effort="inherit",
                              config=cfg(thinking_default="max"))
    assert got == "max"


def test_inherit_stops_at_the_registry_row_before_the_configured_default():
    seen: list[dict] = []
    got = _effort_on_the_wire(seen, tool="delegate", task="q", effort="inherit",
                              config=cfg(thinking_default="max"),
                              entries=(entry(default_effort="low"),))
    assert got == "low"


def test_inherit_defers_to_the_agent_file(tmp_path):
    """The tier that would have been lost. The merge is `effort or agent.effort`, and a
    non-empty string is truthy -- so had `"inherit"` been left in place instead of
    normalised at the boundary, it would have skipped the very file it means to defer to."""
    _agent_in(tmp_path, "helper", "effort: high")
    seen: list[dict] = []
    got = _effort_on_the_wire(
        seen, tool="delegate_to_agent", agent_name="helper", task="q", effort="inherit",
        config=cfg(agents_dir=str(tmp_path), thinking_default="off"),
    )
    assert got == "high"


def test_an_explicit_level_still_overrules_the_agent_file(tmp_path):
    """Unchanged precedence: the argument has always won, and adding `"inherit"` must not
    quietly turn the agent file into the winner for callers who did name a level."""
    _agent_in(tmp_path, "helper", "effort: high")
    seen: list[dict] = []
    got = _effort_on_the_wire(
        seen, tool="delegate_to_agent", agent_name="helper", task="q", effort="off",
        config=cfg(agents_dir=str(tmp_path)),
    )
    assert got == "none"


def test_an_unknown_effort_is_refused_and_the_message_names_inherit():
    """Refused at the boundary rather than four calls deeper in `resolve_effort`, whose
    message names the four levels and cannot mention the fifth word this tool accepts."""
    config = cfg()
    mcp = server.build(config, registry(entry()), DoubleCache(config, ok_handler("served-id-1")))

    async def go():
        async with Client(mcp) as client:
            await client.call_tool("delegate", {"task": "q", "effort": "medium"})

    with pytest.raises(Exception, match="inherit"):
        asyncio.run(go())


# --- the agentic loop's heartbeat, in both of its silent windows --------------------------


def _loop_progress(config, handler, *, before_call=None):
    """Run a two-turn delegation and collect every progress notification the client saw.

    Returns the notifications, so a test can count them against the per-turn ones it would
    have got anyway: a two-turn delegation reports twice on its own, and anything beyond
    that came from the timer.
    """
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))
    seen: list[float] = []

    async def on_progress(progress, total, message):
        seen.append(progress)

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit", "allowed_tools": ["read_file"]}
            )

    if before_call is not None:
        before_call()
    asyncio.run(go())
    return seen


def test_a_slow_turn_keeps_the_client_informed():
    """#58's shape. One turn reports at its top and then says nothing for its whole
    duration, bounded only by `turn_timeout` -- which defaults to exactly the client's
    stdio idle timeout, so a single long turn can outlast it on its own. Lowering that
    default would kill legitimate work, so the fix is the heartbeat.
    """
    config = cfg(keepalive_interval=1)
    seen = _loop_progress(config, slow_chat_handler(2.5))
    assert len(seen) >= 2, (
        f"a turn lasting 2.5s past a 1s keepalive sent {len(seen)} notification(s)")


def test_the_heartbeat_fires_while_a_tool_is_running(tmp_path, monkeypatch):
    """The half a timer task alone does not buy, and the reason this test exists at all.

    `_run_calls` is synchronous and `run_bash` reaches `subprocess.run` in `sandbox.py`, so
    a tool call blocks the event loop for its whole duration -- up to `run_bash_timeout`.
    A keepalive created with `asyncio.create_task` cannot be scheduled during that window,
    so a delegation whose long part is a command would stay exactly as silent as before
    while appearing fixed in every test that only exercises a slow backend.

    The stand-in is `time.sleep` inside the real `_run_calls`, which is what
    `subprocess.run` is: a synchronous block, on the calling thread. Verified to fail
    against the same code without `asyncio.to_thread`.
    """
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    real = loop_module._run_calls

    def slow(*args, **kwargs):
        time.sleep(2.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(loop_module, "_run_calls", slow)

    config = cfg(keepalive_interval=1, workspace_roots=(str(tmp_path),))
    handler = turn_handler(
        tool_call_reply("read_file", {"path": str(target)}),
        chat_reply(content="done"),
    )
    seen = _loop_progress(config, handler)
    # Two turns report on their own; a third notification can only have come from the timer
    # beating through the tool call.
    assert len(seen) > 2, (
        f"a 2.5s tool call past a 1s keepalive produced {len(seen)} notification(s), which "
        "is no more than the two turns report by themselves")


def test_the_loops_heartbeat_stops_when_the_delegation_fails():
    """Cancellation on the paths that are not the happy one.

    The `finally` has to cover every exit from the turn loop, not just the `break`: an
    overflow abort, a backend that refuses, a raising callback. A task left beating after a
    *failed* delegation is worse than after a successful one -- it reports as alive
    something that has already ended, which is exactly the reading ADR-0043's stream exists
    to make possible.

    The waiting happens inside the event loop, as in the one-shot test above: sleeping after
    `asyncio.run` returns proves nothing, because the loop is closed by then.
    """
    config = cfg(keepalive_interval=1)

    async def failing(request):
        await asyncio.sleep(2.5)
        return httpx.Response(500, json={"error": "nope"})

    mcp = server.build(config, registry(entry()), DoubleCache(config, failing))
    seen: list[float] = []

    async def on_progress(progress, total, message):
        seen.append(progress)

    async def go():
        async with Client(mcp, progress_handler=on_progress) as client:
            try:
                await client.call_tool(
                    "delegate", {"task": "q", "effort": "inherit", "allowed_tools": ["read_file"]}
                )
            except Exception:
                pass  # the failure is the point; what it is belongs to another test
            during = len(seen)
            await asyncio.sleep(2.5)
            return during, len(seen)

    during, after = asyncio.run(go())
    assert during, "nothing beat at all, so this cannot detect one that never stops"
    assert after == during, "the heartbeat outlived a delegation that had already failed"


# --- max_turns is a per-call argument like every other agent-file setting -----------------
#
# It was not. `run_delegation` read `max_turns = agent.max_turns` where its three
# neighbours read `x or agent.x`, so the one setting that truncates a run was the one an
# agent file could not be overruled on -- while `delegate_to_agent`'s own description
# already promised that "every explicit argument here wins over the agent file".
#
# Asserted on the resolved budget rather than on the variable: a handler that never stops
# asking for a tool runs until the budget is spent, so the number of backend replies *is*
# the resolved `max_turns`. That is the only place the whole precedence chain has finished.


def _turns_spent(**called_kwargs) -> int:
    """Drive a delegation that never finishes and return how many turns it actually got."""
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=tool_call_reply("read_file", {"path": "/nope"}))

    result = called(handler, allowed_tools=["read_file"], **called_kwargs)
    assert result["hit_turn_limit"], "the handler must exhaust the budget for this to measure it"
    return len(seen)


def test_a_per_call_max_turns_overrules_the_agent_file(tmp_path):
    """The bug. The agent file said 2 and the caller asked for 5; the caller used to lose."""
    _agent_in(tmp_path, "helper", "max_turns: 2")
    spent = _turns_spent(
        tool="delegate_to_agent", agent_name="helper", task="q", effort="inherit",
        max_turns=5, config=cfg(agents_dir=str(tmp_path)),
    )
    assert spent == 5


def test_the_agent_file_still_decides_when_the_caller_says_nothing(tmp_path):
    """The other direction, and the one that makes the first test mean something. A merge
    written as `max_turns or agent.max_turns` could have been written as plain `max_turns`
    and would still pass the test above."""
    _agent_in(tmp_path, "helper", "max_turns: 2")
    spent = _turns_spent(
        tool="delegate_to_agent", agent_name="helper", task="q", effort="inherit",
        config=cfg(agents_dir=str(tmp_path)),
    )
    assert spent == 2


def test_neither_one_means_the_configured_default():
    spent = _turns_spent(tool="delegate", task="q", effort="inherit",
                         config=cfg(max_turns_default=3))
    assert spent == 3


def test_a_callers_number_is_clamped_to_the_hard_cap_in_silence():
    """`resolve_max_turns` clamps rather than refusing, deliberately: the work is
    legitimate and only the number is not. Raising the cap to 100 must not turn that into
    an error for a caller who asks for more."""
    spent = _turns_spent(tool="delegate", task="q", effort="inherit", max_turns=99,
                         config=cfg(max_turns_default=2, max_turns_hard_cap=4))
    # 4 and not 2: a broken clamp that fell through to the default would also be under the
    # cap, so the two numbers are kept apart on purpose.
    assert spent == 4


def test_zero_turns_is_refused_rather_than_clamped_up():
    """The one number that cannot be honoured at all: a delegation with no turns cannot
    produce an answer, so it is a refusal and not a clamp."""
    with pytest.raises(Exception) as e:
        called(chat_handler(), tool="delegate", task="q", effort="inherit", max_turns=0)
    assert "at least 1" in str(e.value)
