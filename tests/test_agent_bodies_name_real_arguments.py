"""Agent and skill bodies must name real MCP arguments, never a renamed one.

The Markdown bodies under ``.claude/`` are what an agent following them reads as the tool
contract: ``files[]``, ``max_turns``, ``project`` and the rest are promises about the
server's tool surface. A rename in ``server.py`` (or a tool dropped from the registry)
strands every copy of the old name in those bodies silently -- nothing at load time looks
at a Markdown file, so the mismatch shows up only as a delegated call the model makes wrong.
Each check here builds the *real* surface from the code and names every inline-code word in
a body that is not on it, and the planted-violation tests prove each check can fire.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import functools
import pathlib
import re

from claude_delegate_local import agents, config, server, tools
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry, Registry

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "claude_delegate_local"

# Words that appear as inline code in real bodies but are not MCP arguments, config keys or
# source names: a binary, a pytest name, a git subcommand, an event name, and literal words
# a body tells its reader to write (`why`, `assumed`). Closed on purpose: a new word that
# is not one of these and not on the real surface is a finding.
ALLOWED = frozenset({
    "bwrap", "line", "tmp_path", "addopts", "log", "priced", "why", "assumed",
})

_INLINE_RE = re.compile(r"`([^`]+)`")
_TOKEN_RE = re.compile(r"^[a-z_][a-z0-9_]*(\[\])?$")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL
)


# --- the real surface ----------------------------------------------------------------


def _mcp_tool_schemas() -> dict[str, dict]:
    """Every MCP tool and its inputSchema properties, from a server built like test_server.

    The server is built the way tests/test_server.py builds it -- a Config, a one-entry
    Registry and no backend cache (nothing here calls a tool, so no socket is opened).
    """
    cfg = Config(workspace_roots=(".",), admission_idle_hold=0.0)
    entry = ModelEntry(
        key="flash", base_url="http://example.com:8000", served_model_id="served-id-1"
    )
    registry = Registry(entries={entry.key: entry}, default_key=entry.key)
    mcp = server.build(cfg, registry)

    async def list_tools():
        return await mcp.list_tools()

    schemas: dict[str, dict] = {}
    for tool in asyncio.run(list_tools()):
        props = dict(tool.parameters.get("properties") or {})
        schemas[tool.name] = {
            "properties": set(props),
            "order": list(props),
            "required": set(tool.parameters.get("required") or ()),
        }
    return schemas


def _source_defined_names() -> set[str]:
    """Every function, async function and class defined in the package source."""
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


@functools.lru_cache(maxsize=1)
def known_names() -> frozenset[str]:
    """Every name a body may legitimately backtick: the whole real tool and config surface."""
    names: set[str] = set(_source_defined_names())

    for name, schema in _mcp_tool_schemas().items():
        names.add(name)
        names |= schema["properties"]

    names |= set(server._DELEGATION_RESULT.get("properties", {}))
    names |= set(tools.REGISTRY)
    names |= {field.name for field in dataclasses.fields(Config)}
    names |= set(agents.KNOWN_FIELDS)
    names |= set(config.EFFORT_LEVELS)
    names |= {"true", "false", "null"}
    return frozenset(names)


# --- the checks -----------------------------------------------------------------------


def _inline_spans(body: str) -> list[str]:
    """Every single-backtick span outside fenced ``` blocks."""
    spans: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _INLINE_RE.finditer(line):
            spans.append(match.group(1))
    return spans


def _call_candidates(body: str) -> list[str]:
    """Inline spans, plus whole lines of fenced blocks: where a call expression can live."""
    candidates: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            candidates.append(line)
        else:
            for match in _INLINE_RE.finditer(line):
                candidates.append(match.group(1))
    return candidates


def unknown_names(body: str, known: set[str] | frozenset[str]) -> list[str]:
    """Inline-code words in the body that are neither a known name nor an ALLOWED word."""
    found: set[str] = set()
    for span in _inline_spans(body):
        if not _TOKEN_RE.match(span):
            continue
        token = span.removesuffix("[]")
        if token.startswith("test_") or token.startswith("__"):
            continue
        if token in known or token in ALLOWED:
            continue
        found.add(token)
    return sorted(found)


def _extract_calls(text: str, tool_names: set[str]) -> list[ast.Call]:
    """The ``name(...)`` expressions in ``text``, parsed, for any MCP tool name."""
    ordered = sorted(tool_names, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in ordered) + r")\s*\(")
    calls: list[ast.Call] = []
    for match in pattern.finditer(text):
        start = match.start()
        i = match.end()  # just past the '('
        depth = 1
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        expr = text[start:i]
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            continue
        if isinstance(tree.body, ast.Call):
            calls.append(tree.body)
    return calls


def bad_calls(body: str, schemas: dict[str, dict]) -> list[str]:
    """Call expressions in the body that use an unknown keyword or omit a required one."""
    reports: list[str] = []
    for candidate in _call_candidates(body):
        for call in _extract_calls(candidate, set(schemas)):
            name = call.func.id
            props = schemas[name]["properties"]
            order = schemas[name]["order"]
            required = schemas[name]["required"]
            kwargs = {kw.arg for kw in call.keywords if kw.arg is not None}
            for keyword in call.keywords:
                if keyword.arg is not None and keyword.arg not in props:
                    reports.append(f"{name}: unknown keyword {keyword.arg!r}")
            positional = len(call.args)
            for req in sorted(required):
                present = req in kwargs
                if not present and req in order and order.index(req) < positional:
                    present = True
                if not present:
                    reports.append(f"{name}: missing required {req!r}")
    return reports


def _frontmatter(text: str) -> str:
    """The opening YAML block, or '' when there is none."""
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else ""


def bad_allowed_tools(frontmatter: str) -> list[str]:
    """``allowed_tools`` entries that are not delegated-model registry tools."""
    bad: set[str] = set()
    for match in re.finditer(r"allowed_tools\s*:\s*\[([^\]]*)\]", frontmatter):
        for raw in match.group(1).split(","):
            item = raw.strip()
            if item and item not in tools.REGISTRY:
                bad.add(item)
    return sorted(bad)


# --- the bodies -----------------------------------------------------------------------


def body_files() -> list[pathlib.Path]:
    """Every agent and skill body: the files this check exists to keep honest."""
    found = sorted((ROOT / ".claude/agents").glob("*.md"))
    found += sorted((ROOT / ".claude/delegate-agents").glob("*.md"))
    found += sorted((ROOT / ".claude/skills").rglob("*.md"))
    return found


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


# --- the real bodies ------------------------------------------------------------------


def test_there_are_bodies_to_check() -> None:
    """Guards the collector: an empty glob must never read as an empty set of failures."""
    assert len(body_files()) >= 5


def test_every_real_body_names_real_arguments() -> None:
    """The check itself. A body that backticks a renamed argument is the bug this guards.

    Each of the three inspectors must be silent over every real body. A finding names the
    file and the token, so the fix is the body's word, not the check's allowance.
    """
    known = known_names()
    schemas = _mcp_tool_schemas()
    problems: list[str] = []
    for path in body_files():
        body = path.read_text(encoding="utf-8")
        for token in unknown_names(body, known):
            problems.append(f"{_rel(path)}: inline-code name {token!r} is not a real argument")
        for report in bad_calls(body, schemas):
            problems.append(f"{_rel(path)}: {report}")
        for bad in bad_allowed_tools(_frontmatter(body)):
            problems.append(f"{_rel(path)}: allowed_tools names {bad!r}, not a registry tool")
    assert not problems, "agent/skill bodies name non-existent arguments:\n" + "\n".join(problems)


# --- negative controls: every check must be able to fire -------------------------------


def test_unknown_names_reports_a_planted_wait_secs() -> None:
    """A backticked word that is not an argument is a finding."""
    body = "Collect the run once it has settled with `wait_secs`."
    assert "wait_secs" in unknown_names(body, known_names())


def test_bad_calls_reports_the_unknown_keyword() -> None:
    """`collect` takes `wait_seconds`, so `wait_secs` is a keyword it does not have."""
    body = 'Collect it with `collect(handle="h", wait_secs=5)`.'
    reports = bad_calls(body, _mcp_tool_schemas())
    assert any("wait_secs" in report for report in reports), reports


def test_bad_calls_reports_the_missing_required() -> None:
    """`delegate` demands `task`; a call that omits it is a promise the model cannot keep."""
    body = 'Ask `delegate(effort="low")`.'
    reports = bad_calls(body, _mcp_tool_schemas())
    assert any("task" in report and "missing" in report for report in reports), reports


def test_a_complete_delegate_call_reports_nothing() -> None:
    """The positive side: every argument real, every required one present, so nothing fires."""
    body = 'Ask `delegate(task="t", effort="low")`.'
    assert bad_calls(body, _mcp_tool_schemas()) == []


def test_bad_allowed_tools_reports_a_planted_unknown_tool() -> None:
    """`grep_files` is not a registry tool; the real name is `search_files`."""
    frontmatter = "name: probe\nallowed_tools: [read_file, grep_files]\n"
    assert bad_allowed_tools(frontmatter) == ["grep_files"]
