"""The tools the delegated model may call, and the two places `allowed_tools` is enforced.

Not to be confused with `server.py`'s `@mcp.tool` functions. Those are the surface *Claude*
calls; these are the surface the local model calls, inside a delegation. The two never meet.

The registry is a fixed table rather than a decorator that mutates module state on import.
A tool schema is part of the prompt prefix the cluster caches (ADR-0011), so the declared
set has to be a function of its inputs and nothing else -- import order included.

`allowed_tools` is checked twice, and the second time is the one that matters. Filtering the
declared list is advisory: a model can name a tool it was never offered, and some do. So
`declared_tools` decides what is *offered* and `execute_tool` decides what is *run*, from the
same set, and neither trusts the other. They sit next to each other here so that adding a
tool cannot quietly add it to only one.

The resolved set is a parameter, never a config field. It is per-agent (M6 frontmatter) or
per-call; a server-wide default would be a config default living outside `config.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .backends.base import ToolResultBlock, ToolSpec, ToolUseBlock
from .config import Config
from .context import decode_text
from .paths import PathPolicyError, PathRefused, resolve_all


class ToolRefused(Exception):
    """A tool declined the call. Reported to the model, never raised past `execute_tool`."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """One tool: what the model is told, and what actually runs.

    `cacheable` says whether repeating this call with identical arguments, inside one
    delegation, must produce the same answer -- which is what lets the turn loop serve a
    repeat from what it already has instead of paying for it twice. It is a property of
    the tool, so it is declared with the tool; a loop that decided this by name would be
    keeping a second copy of a fact about `tools.py` somewhere else.

    False is the safe answer and the default. A tool with side effects is never cacheable,
    and it also invalidates what was cached: a file read before a write and read again
    after it has two different correct answers, and serving the first one twice would hand
    the model a stale file it has every reason to believe is current.
    """

    spec: ToolSpec
    handler: Callable[[Config, dict[str, object]], str]
    cacheable: bool = False


# --- argument helpers -------------------------------------------------------------------
#
# The model supplies these, so they are checked rather than trusted. A wrong type here is a
# refusal the model can read and correct on its next turn, not a traceback that ends the
# delegation.


def _text_arg(args: dict[str, object], name: str, *, required: bool = True) -> str:
    value = args.get(name)
    if value is None:
        if required:
            raise ToolRefused(f"{name!r} is required.")
        return ""
    if not isinstance(value, str):
        raise ToolRefused(f"{name!r} must be a string, not {type(value).__name__}.")
    return value


def _int_arg(args: dict[str, object], name: str, default: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolRefused(f"{name!r} must be a whole number.")
    if value < 0:
        raise ToolRefused(f"{name!r} must not be negative.")
    return value


def _one_path(cfg: Config, given: str, *, must_exist: bool) -> str:
    """Run the caller's path through the four-layer policy and return the resolved one.

    `paths.py` governs both file tools and the sandbox governs none of them: only `run_bash`
    is ever confined, so this is the whole control for a read or a write (ADR-0010).
    """
    resolved = resolve_all(cfg, [given], must_exist=must_exist)
    if not resolved:
        # Layer 4 dropped it without a refusal only if the caller passed nothing at all.
        raise ToolRefused(f"{given!r} resolved to no file.")
    return resolved[0].posix


# --- the tools --------------------------------------------------------------------------


def _read_file(cfg: Config, args: dict[str, object]) -> str:
    real = _one_path(cfg, _text_arg(args, "path"), must_exist=True)
    offset = _int_arg(args, "offset", 0)
    try:
        with open(real, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        raise ToolRefused(f"could not read it: {e.strerror or e}") from e

    text, why = decode_text(raw)
    if text is None:
        raise ToolRefused(why)

    total = len(text)
    if offset >= total and total:
        raise ToolRefused(
            f"offset {offset} is past the end; the file is {total} characters.")

    window = text[offset:offset + cfg.max_read_chars]
    end = offset + len(window)
    if end >= total:
        return window
    # The true total and the next offset, so a continuation is a second range rather than a
    # second whole read. Appended to the result, never to the system prompt: the prefix must
    # stay byte-identical across calls (ADR-0011).
    return (
        f"{window}\n\n[truncated: characters {offset} to {end} of {total}. "
        f"Call read_file again with offset={end} for the rest.]"
    )


def _write_file(cfg: Config, args: dict[str, object]) -> str:
    given = _text_arg(args, "path")
    content = _text_arg(args, "content")
    encoded = content.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        # Refused, not truncated. A silently shortened file is a corrupted one, and the
        # model would have no way to know -- the same reasoning as context.py's whole-file
        # skip on prefetch.
        raise ToolRefused(
            f"{len(encoded)} bytes exceeds the {cfg.max_write_bytes}-byte limit for one "
            f"write. Write it in parts.")

    real = _one_path(cfg, given, must_exist=False)
    existed = os.path.exists(real)
    try:
        with open(real, "wb") as fh:
            fh.write(encoded)
    except OSError as e:
        raise ToolRefused(f"could not write it: {e.strerror or e}") from e
    verb = "Overwrote" if existed else "Created"
    return f"{verb} {real} ({len(encoded)} bytes)."


def _run_bash(cfg: Config, args: dict[str, object]) -> str:
    """Always refuses. The sandbox does not exist yet, and unconfined is not the fallback.

    ADR-0010: upstream logs a warning and runs the command anyway. A security control that
    silently degrades to nothing is worse than one that is absent, because it is believed.
    `sandbox.py` is M5.

    `sandbox.py` now exists, but nothing here calls it yet: the mount-level secret denylist
    is unbuilt, so the route stays closed and this refusal stays unconditional. Reading a
    sandbox setting here would mark it live in the generated reference while nothing acts on
    it, and would imply a branch that does not exist.
    """
    _text_arg(args, "command")
    raise ToolRefused(
        "run_bash is refused: the sandbox is not built yet (M5), and this server does not "
        "run shell commands unconfined as a fallback (ADR-0010). Use read_file and "
        "write_file, and leave anything needing a shell to the caller.")


READ_FILE = RegisteredTool(
    spec=ToolSpec(
        name="read_file",
        description=(
            "Read a UTF-8 text file from the workspace. Paths must be absolute. Long files "
            "come back in one range at a time: the result states the total length and the "
            "offset to continue from, so read the next range rather than the file again. "
            "Refused for a path outside the workspace, an unlisted extension, a file git "
            "ignores, or anything that is not text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "offset": {
                    "type": "integer",
                    "description": "Character offset to start at. Omit to start at 0.",
                },
            },
            "required": ["path"],
        },
    ),
    handler=_read_file,
    cacheable=True,
)

WRITE_FILE = RegisteredTool(
    spec=ToolSpec(
        name="write_file",
        description=(
            "Write a UTF-8 text file in the workspace, creating it or replacing it whole. "
            "Paths must be absolute and the containing directory must already exist. The "
            "same path rules as read_file apply. Content over the size limit is refused "
            "rather than truncated, so write large files in parts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "content": {"type": "string", "description": "The complete file contents."},
            },
            "required": ["path", "content"],
        },
    ),
    handler=_write_file,
)

RUN_BASH = RegisteredTool(
    spec=ToolSpec(
        name="run_bash",
        description=(
            "Run a shell command. CURRENTLY REFUSES EVERY CALL: the sandbox that would "
            "confine it is not built, and this server will not run commands unconfined "
            "instead. Do not plan around it; use read_file and write_file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."},
            },
            "required": ["command"],
        },
    ),
    handler=_run_bash,
)

# Insertion order is the declared order, and it is fixed here rather than sorted at use.
REGISTRY: dict[str, RegisteredTool] = {
    t.spec.name: t for t in (READ_FILE, WRITE_FILE, RUN_BASH)
}
ALL_TOOL_NAMES: frozenset[str] = frozenset(REGISTRY)

# Implemented, but not offered, because it cannot work: `run_bash` refuses every call until
# `sandbox.py` exists (ADR-0010). Declaring it anyway spends a turn to learn that -- and
# ADR-0016 measured that the first turn is already often wasted on orientation, so this
# would be the second. The registry keeps it because the refusal is the behaviour under
# test, and `execute_tool` keeps checking it because a model can call what it was never
# offered; what changes here is only what gets *declared*.
#
# Withheld server-wide rather than left to each caller: "the sandbox is not built" is a
# fact about this server, not a preference of one delegation, so no `allowed_tools`
# argument can opt back in. When M5 lands, this set empties and both sites follow.
WITHHELD_TOOL_NAMES: frozenset[str] = frozenset({"run_bash"})


def available_tool_names() -> frozenset[str]:
    """What this server can offer today, as opposed to what it implements."""
    return ALL_TOOL_NAMES - WITHHELD_TOOL_NAMES


def resolve_allowed(requested: Iterable[str] | None) -> frozenset[str]:
    """The resolved set for one delegation: what was asked for, minus what cannot work.

    `None` means "whatever is available", which is the default a caller gets by not
    choosing. An explicit set is narrowed, never widened -- intersecting rather than
    unioning is what stops a caller naming a tool this server withholds, or one it does
    not implement at all, and getting it.
    """
    available = available_tool_names()
    if requested is None:
        return available
    return frozenset(requested) & available


# --- the two enforcement sites ------------------------------------------------------------


def declared_tools(allowed: Iterable[str]) -> tuple[ToolSpec, ...]:
    """Site one: what the model is offered.

    Registry order, not the caller's and not sorted, so the same resolved set always renders
    the same bytes. Tool schemas sit in the cached prefix, and an order that varied per call
    would cost a fresh prefill for nothing (ADR-0011).
    """
    permitted = set(allowed)
    return tuple(t.spec for name, t in REGISTRY.items() if name in permitted)


def execute_tool(
    cfg: Config, call: ToolUseBlock, allowed: Iterable[str]
) -> ToolResultBlock:
    """Site two: what actually runs.

    Checked against `allowed` independently of what was declared, because the declared list
    is a suggestion the model can ignore -- and one that names a tool it was never offered
    is exactly the case worth catching.

    A refusal comes back as an error result rather than an exception. `PathRefused` aborting
    the whole delegation is right for prefetch, where nothing has been spent yet; mid-loop it
    would throw away every turn already paid for because the model made one bad call. Telling
    it no and letting it try again is cheaper than starting over.
    """
    if call.name not in set(allowed):
        return ToolResultBlock(
            tool_use_id=call.id,
            content=(
                f"{call.name!r} is not available in this delegation. Available: "
                f"{', '.join(sorted(set(allowed))) or 'none'}."
            ),
            is_error=True,
        )
    tool = REGISTRY.get(call.name)
    if tool is None:
        return ToolResultBlock(
            tool_use_id=call.id,
            content=f"{call.name!r} is not a tool this server implements.",
            is_error=True,
        )
    try:
        return ToolResultBlock(tool_use_id=call.id, content=tool.handler(cfg, call.input))
    except (ToolRefused, PathRefused, PathPolicyError) as e:
        return ToolResultBlock(tool_use_id=call.id, content=str(e), is_error=True)
