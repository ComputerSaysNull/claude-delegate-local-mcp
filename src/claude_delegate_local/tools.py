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
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import sandbox
from .backends.base import BashOutcome, ToolResultBlock, ToolSpec, ToolUseBlock
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
    handler: Callable[[Config, dict[str, object]], str | BashResult]
    cacheable: bool = False


@dataclass(frozen=True, slots=True)
class BashResult:
    """A handler's text plus what the server measured, on the way to the result block.

    Only `run_bash` returns one. Every other handler still returns a bare string, so this
    widens one tool's contract rather than all of them.
    """

    text: str
    outcome: BashOutcome
    is_error: bool = False


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


def _capped(cfg: Config, result: sandbox.SandboxResult) -> str:
    """stdout and stderr, labelled, cut to the cap with the true length stated.

    Labelled because a command that printed nothing and one that printed to stderr are
    different facts, and a model reading them merged cannot tell which it has. Cut rather
    than refused -- unlike `write_file`, where a short file is a corrupt one -- because a
    truncated log is still evidence, and the tail is the part worth keeping: a build says
    what went wrong on its last line, not its first.
    """
    parts = []
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr.rstrip()}")
    text = "\n\n".join(parts) or "(no output)"
    if len(text) <= cfg.max_bash_output_chars:
        return text
    kept = text[-cfg.max_bash_output_chars:]
    return (
        f"[truncated: {len(text)} characters of output, showing the last "
        f"{len(kept)}.]\n{kept}"
    )


def _run_bash(cfg: Config, args: dict[str, object]) -> BashResult:
    """Run one command in the sandbox, and report what the server saw it do.

    ADR-0010: upstream logs a warning and runs the command unconfined when bubblewrap is
    missing. That never happens here. Every path out of this function that did not reach a
    real process exit reports `exit_code=None`, and `ran` separates "nothing ran" from "it
    ran and returned nothing to say" -- the model's account of the command is not evidence,
    and 0 is a real exit code that must not be inventable by a refusal (ADR-0007).

    Refusals return rather than raise, so the ledger can count an attempted call. A model
    that tried ten commands and was refused ten times has not run zero commands, and
    `tool_calls` beside it in the same ledger already counts attempts.
    """
    try:
        command = _text_arg(args, "command")
    except ToolRefused as e:
        return BashResult(str(e), BashOutcome(exit_code=None), is_error=True)

    try:
        result = sandbox.run(cfg, sandbox.SandboxRequest(
            command=command,
            home=sandbox.resolve_home(cfg),
            # No caller supplies a workspace bind yet, so the command's working directory is
            # the sandbox HOME. `discover_secret_shadows` already covers a workdir when one
            # arrives; nothing here has to change for it.
            workdir=None,
            network=False,
            extra_binds=sandbox.probe_toolchain_binds(cfg),
            env=sandbox.resolve_env(cfg),
        ))
    except (sandbox.SandboxUnavailable, sandbox.SecretShadowIncomplete) as e:
        return BashResult(str(e), BashOutcome(exit_code=None), is_error=True)

    if result.timed_out:
        # Said in words, not left to a null exit code. "No exit code" must not be readable
        # as success by a model summarising its own run.
        return BashResult(
            f"Timed out after {cfg.run_bash_timeout}s and was killed. Output up to that "
            f"point:\n\n{_capped(cfg, result)}",
            BashOutcome(exit_code=None, timed_out=True, ran=True),
            is_error=True,
        )

    body = _capped(cfg, result)
    return BashResult(
        f"exit {result.exit_code}\n\n{body}",
        BashOutcome(exit_code=result.exit_code, ran=True),
        is_error=result.exit_code != 0,
    )


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
            "Run a shell command, confined: no network, an empty filesystem apart from a "
            "scratch HOME and a read-only toolchain, and your real home directory absent "
            "rather than merely unreadable. Commands time out and are killed. The server "
            "reports the real exit code it observed, so do not describe a command as having "
            "succeeded when the result says otherwise. To change a file's text, prefer "
            "write_file, which replaces it whole."
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

# Empty, as of M5. It held `run_bash` from M4 until the sandbox could confine it and the
# mount-level denylist could cover secrets inside what it binds; both exist now, so the
# route is open and nothing here narrows the declared set.
#
# It stays empty. `run_bash` is withheld on a host without bubblewrap, but that is a fact
# about the host and is asked for in `available_tool_names` rather than recorded here: a
# constant cannot answer a question whose answer differs per machine. This set is for a
# tool this server withholds *everywhere*, which is a narrower thing than it looks and is
# why emptying it was right.
#
# Kept rather than deleted, and deliberately. Withholding is how this server says "this
# tool exists and cannot work today", which is a different statement from a caller's
# `allowed_tools` narrowing one delegation -- a fact about the server, not a preference.
# Deleting the mechanism would mean rebuilding it under pressure the next time something is
# implemented before it is safe. Note what it never was: withholding narrows only what is
# *declared*, and `execute_tool` checks its own allowed set regardless, because a model can
# call a tool it was never offered. Emptying this set does not weaken that second check,
# because that check never consulted this set.
WITHHELD_TOOL_NAMES: frozenset[str] = frozenset()


def available_tool_names(cfg: Config) -> frozenset[str]:
    """What this server can offer today, as opposed to what it implements.

    Asks rather than remembers. `run_bash` needs bubblewrap, and whether bubblewrap is
    present is a fact about the host this process is running on -- not something a constant
    written at import time can know. M5 emptied `WITHHELD_TOOL_NAMES` and left this function
    taking no `Config`, so on a host without `bwrap` the tool was declared and then refused
    every call: a whole turn spent learning what the server already knew, and ADR-0016
    measured that the first turn is often already lost to orientation, making this the
    second (JOURNAL 2026-08-29).

    The same condition `sandbox.run` refuses on, checked where the set is resolved instead
    of after a turn has been paid for. One condition, two places it can be observed, and
    this is the cheaper one.
    """
    names = ALL_TOOL_NAMES - WITHHELD_TOOL_NAMES
    if not sandbox.available(cfg):
        names -= {"run_bash"}
    return names


def resolve_allowed(requested: Iterable[str] | None, cfg: Config) -> frozenset[str]:
    """The resolved set for one delegation: what was asked for, minus what cannot work.

    `None` means "whatever is available", which is the default a caller gets by not
    choosing. An explicit set is narrowed, never widened -- intersecting rather than
    unioning is what stops a caller naming a tool this server withholds, one the host
    cannot run, or one it does not implement at all, and getting it.
    """
    available = available_tool_names(cfg)
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


# A shell command that rewrites file text, which `write_file` does better. Upstream found a
# system-prompt instruction did not stop the pattern on retry, so the note is appended to the
# offending call's own result instead -- next to the evidence, in the turn that has to decide
# what to do next (ADR-0024).
#
# In-place editors are named explicitly rather than caught by a general "looks like editing"
# rule, because the cost of a false positive is a note the model can ignore and the cost of a
# false negative is nothing at all. Redirection is the one general case, and it excludes the
# device targets and fd duplications that are not file edits at all.
_IN_PLACE = re.compile(
    r"(?:^|[\s;&|(])(?:sed|perl|ruby|gawk|awk)\s+(?:-\w*\s+)*-[a-z]*i\b"
    r"|(?:^|[\s;&|(])patch\b"
    r"|(?:^|[\s;&|(])tee\b(?!\s+/dev/)",
)
_REDIRECT = re.compile(r">>?\s*(?![&/]dev/)(?!&)([^\s;&|>]+)")

STEER = (
    "[note: this command rewrites file text through the shell. write_file is available in "
    "this delegation and replaces a file whole, which avoids the quoting, escaping and "
    "partial-match mistakes that in-place edits fail silently on. Advisory only: the command "
    "above ran and its result stands.]"
)


def _rewrites_text(command: str) -> bool:
    """Whether a shell command looks like it patches file text.

    Advisory, so this errs toward quiet: a missed case costs nothing, and a false positive
    costs one line the model may ignore. It must never be read as a reason to block.
    """
    if _IN_PLACE.search(command):
        return True
    for target in _REDIRECT.findall(command):
        if not target.startswith("/dev/"):
            return True
    return False


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
    permitted = set(allowed)
    if call.name not in permitted:
        return ToolResultBlock(
            tool_use_id=call.id,
            content=(
                f"{call.name!r} is not available in this delegation. Available: "
                f"{', '.join(sorted(permitted)) or 'none'}."
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
        produced = tool.handler(cfg, call.input)
    except (ToolRefused, PathRefused, PathPolicyError) as e:
        return ToolResultBlock(tool_use_id=call.id, content=str(e), is_error=True)
    if isinstance(produced, BashResult):
        text = produced.text
        # Gated on the resolved set this function enforces, never on what was declared: the
        # declared list is a suggestion, and steering toward a tool the executor would refuse
        # would be worse than staying quiet. `permitted` is that set (ADR-0024).
        if "write_file" in permitted and _rewrites_text(str(call.input.get("command") or "")):
            text = text + "\n\n" + STEER
        return ToolResultBlock(
            tool_use_id=call.id,
            content=text,
            is_error=produced.is_error,
            bash=produced.outcome,
        )
    return ToolResultBlock(tool_use_id=call.id, content=produced)
