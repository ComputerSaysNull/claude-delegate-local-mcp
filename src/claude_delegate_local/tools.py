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

import fnmatch
import os
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import sandbox
from .backends.base import BashOutcome, ToolResultBlock, ToolSpec, ToolUseBlock
from .config import Config
from .context import decode_text
from .paths import (
    PathPolicyError,
    PathRefused,
    ResolvedPath,
    load_secret_globs,
    open_resolved,
    resolve_all,
    resolve_permitted,
    resolve_search_root,
    resolved_roots,
    secret_match,
)


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
    handler: Callable[..., str | BashResult]
    cacheable: bool = False
    # Whether this tool's handler takes the delegation's `BashPolicy` as a third argument.
    # Declared with the tool for the same reason `cacheable` is: it is a fact about the
    # tool, and an executor deciding it by name would keep a second copy of that fact
    # somewhere else. Only `run_bash` enters the sandbox, so only `run_bash` sets it, and
    # widening one tool's signature beats adding an ignored parameter to the other two.
    wants_policy: bool = False
    # Whether this tool can change anything outside the server's own memory. Declared with
    # the tool, and for a sharper reason than the two above: `READ_ONLY_TOOL_NAMES` is
    # derived from it, and that set is what makes `delegate_readonly`'s readOnlyHint a
    # property of the tool rather than a claim about how it is usually called. A hand-kept
    # list would go stale the first time a writing tool was added, silently, and the
    # annotation would still be advertised. False is NOT the safe default here -- a new
    # tool that writes must say so -- so every writing tool sets it explicitly and the
    # negative-test in tests/test_tools.py asserts the derived set against the registry.
    writes: bool = False


@dataclass(frozen=True, slots=True)
class BashPolicy:
    """What one delegation may do inside the sandbox: where, with what, and on whose network.

    Per delegation, never a config field -- for the same reason `allowed_tools` is not one.
    These come from an agent file or a call argument, and a server-wide default for them
    would be a config default living outside `config.py`.

    The default is the pre-M6 behaviour exactly: no workspace bound, no network, and only
    the toolchain binds the server probes for itself. A caller that says nothing gets a
    sandbox that can reach nothing of theirs, which is the right way round.
    """

    workdir: str | None = None
    network: bool = False
    extra_binds: tuple[str, ...] = ()


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


def _one_path(cfg: Config, given: str, *, must_exist: bool) -> ResolvedPath:
    """Run the caller's path through the four-layer policy and return the resolved entry.

    `paths.py` governs both file tools and the sandbox governs none of them: only `run_bash`
    is ever confined, so this is the whole control for a read or a write (ADR-0010).

    The entry, not its `.posix`. Handing back a string is what let three handlers check
    a name and then open a file, which are the same thing only for as long as nothing
    changes in between; `open_resolved` is the only sanctioned way to open what this
    returns (ADR-0049).
    """
    resolved = resolve_all(cfg, [given], must_exist=must_exist)
    if not resolved:
        # Layer 4 dropped it without a refusal only if the caller passed nothing at all.
        raise ToolRefused(f"{given!r} resolved to no file.")
    return resolved[0]


# --- the tools --------------------------------------------------------------------------


def _read_file(cfg: Config, args: dict[str, object]) -> str:
    entry = _one_path(cfg, _text_arg(args, "path"), must_exist=True)
    # 1-based, because every other thing that cites a line is: an editor, a traceback, a
    # reviewer. `offset` counted characters, which meant the model could neither be pointed
    # at lines 400 to 460 nor cite what it had read, and one agent file worked around it by
    # being told to quote instead and warned that it is never shown a line number.
    start = _int_arg(args, "start_line", 1)
    if start < 1:
        raise ToolRefused(
            f"start_line {start} is not a line number; lines are counted from 1.")

    # Sized from the descriptor rather than from the path, which is still what
    # `max_file_read_bytes` means by checking "BEFORE reading": opening a file loads none
    # of it, so the ceiling refuses a multi-gigabyte file without reading it, and it now
    # measures the file actually being held rather than whatever the path named a moment
    # earlier. One setting, one meaning, all three consumers.
    try:
        opened = open_resolved(entry, "rb")
    except OSError as e:
        raise ToolRefused(f"could not read it: {e.strerror or e}") from e

    try:
        with opened.handle as fh:
            nbytes = os.fstat(fh.fileno()).st_size
            if nbytes > cfg.max_file_read_bytes:
                # Refused rather than paged. At `max_read_chars` a file this size needs
                # more calls than the turn budget allows, so offering pagination would
                # spend the delegation getting nowhere; a shell that can cut out the
                # part actually wanted is the real answer.
                raise ToolRefused(
                    f"it is {nbytes} bytes, over the {cfg.max_file_read_bytes}-byte "
                    f"ceiling, so it was not read at all. Use run_bash with sed, head "
                    f"or grep to cut out the part you need."
                )
            raw = fh.read(cfg.max_file_read_bytes)
    except OSError as e:
        raise ToolRefused(f"could not read it: {e.strerror or e}") from e

    text, why = decode_text(raw)
    if text is None:
        raise ToolRefused(why)

    # `splitlines()` and not `split("\n")`: the latter invents a trailing empty line for
    # every file that ends in a newline, which is most of them, and the model would be told
    # the file is one line longer than it is. Line endings are dropped here and re-joined
    # with "\n" below, so a CRLF file reads the same on either side of the boundary -- the
    # numbering is what the caller asked for, not a byte-exact echo.
    lines = text.splitlines()
    total = len(lines)
    if start > total and total:
        raise ToolRefused(
            f"start_line {start} is past the end; the file has {total} lines.")

    # The window is still bounded in characters by `max_read_chars`, because that is what
    # bounds a *reply*, and a file of very long lines would otherwise blow past it. What
    # changed is that it now stops on a line boundary: half a line, numbered, would be
    # worse than no numbering at all, because the number would be a lie about what follows.
    out: list[str] = []
    width = len(str(total))
    used = 0
    index = start
    for index in range(start, total + 1):
        rendered = f"{index:>{width}}  {lines[index - 1]}"
        if out and used + len(rendered) + 1 > cfg.max_read_chars:
            break
        out.append(rendered)
        used += len(rendered) + 1
    else:
        # Ran to the end without breaking, so `index` is the last line, not the next one.
        index = total + 1

    body = "\n".join(out)
    if index > total:
        return body
    # The true total and the next `start_line`, so a continuation is a second range rather
    # than a second whole read. Appended to the result, never to the system prompt: the
    # prefix must stay byte-identical across calls (ADR-0011).
    return (
        f"{body}\n\n[truncated: lines {start} to {index - 1} of {total}. "
        f"Call read_file again with start_line={index} for the rest.]"
    )


def _search_candidates(cfg: Config, scope: str, name_glob: str) -> tuple[list[str], bool]:
    """Enumerate files under `scope` that are worth asking the policy about.

    Enumeration only. Nothing here decides whether a file may be *read* -- that is
    `resolve_permitted`'s, over the same four layers the file tools use. What this does is
    avoid handing the policy a hundred thousand candidates: the extension allowlist and the
    caller's glob are cheap string tests, and pruning a denylisted directory keeps the walk
    out of `.git` entirely rather than discovering it a loose object at a time.

    Returns the candidates and whether the scan cap was reached.
    """
    globs = load_secret_globs(cfg)
    allowed_exts = {e.lower() for e in cfg.ext_allowlist}
    found: list[str] = []
    scanned = 0
    capped = False

    for dirpath, dirnames, filenames in os.walk(scope):
        # Prune in place, and skip symlinks rather than following them. `os.walk` does not
        # follow them by default; the explicit skip is what stops the same tree being
        # enumerated twice through two names.
        kept = []
        for name in sorted(dirnames):
            full = posixpath.join(dirpath, name)
            if os.path.islink(full) or secret_match(full, globs) is not None:
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            if scanned >= cfg.search_max_files_scanned:
                capped = True
                return found, capped
            full = posixpath.join(dirpath, name)
            if os.path.islink(full):
                continue
            if os.path.splitext(name)[1].lower() not in allowed_exts:
                continue
            if name_glob and not fnmatch.fnmatchcase(name, name_glob):
                continue
            scanned += 1
            found.append(full)

    return found, capped


@dataclass(frozen=True, slots=True)
class _Hits:
    """What a search found, and whether it stopped early. Rendering is separate."""

    lines: tuple[str, ...]
    files: int
    truncated: bool


def _search_hits(cfg: Config, paths, needle, max_results: int) -> _Hits:
    """Read each permitted file and collect matching lines, bounded twice.

    Bounded by `max_results` because a caller asked, and by `max_read_chars` because that
    is what bounds any tool reply -- a pattern matching every line of a large file would
    otherwise return the file.
    """
    lines: list[str] = []
    files = 0
    used = 0
    truncated = False

    for item in paths:
        if len(lines) >= max_results:
            return _Hits(tuple(lines), files, True)
        try:
            with open_resolved(item, "rb").handle as fh:
                blob = fh.read(cfg.max_file_read_bytes)
        except (OSError, PathRefused):
            # Vanished, unreadable, or no longer the file the policy approved, between
            # the walk and here. Dropped rather than refused, matching
            # `resolve_permitted`: nobody named this path, so it is not a result rather
            # than an error.
            continue
        text, _ = decode_text(blob)
        if text is None:
            continue

        hits = 0
        for number, line in enumerate(text.splitlines(), start=1):
            if not needle.search(line):
                continue
            # "<file> line <n>: <text>", never "<file>:<n>" -- with a four-digit number
            # that second shape reads as a host and a port, which this project refuses
            # wherever text can reach a commit message or a pull request body.
            rendered = f"{item.posix} line {number}: {line.strip()}"
            if used + len(rendered) + 1 > cfg.max_read_chars or len(lines) >= max_results:
                truncated = True
                break
            lines.append(rendered)
            used += len(rendered) + 1
            hits += 1
        if hits:
            files += 1
        if truncated:
            break

    return _Hits(tuple(lines), files, truncated)


def _search_report(cfg: Config, hits: _Hits, *, capped: bool) -> str:
    """The result, with every reason it might be incomplete stated in it.

    A truncated search that reads like an exhaustive one is the failure worth avoiding:
    the model will conclude a symbol does not exist, and say so confidently.
    """
    tail: list[str] = [f"{len(hits.lines)} matching line(s) in {hits.files} file(s)."]
    if hits.truncated:
        tail.append(
            "Stopped early, so there are more matches than these -- raise max_results, or "
            "narrow the search with path or glob."
        )
    if capped:
        tail.append(
            f"Only the first {cfg.search_max_files_scanned} files were opened, so this is "
            "not exhaustive. Narrow it with path or glob."
        )
    return "\n".join(hits.lines) + "\n\n" + " ".join(tail)


def _search_files(cfg: Config, args: dict[str, object]) -> str:
    """Grep the workspace, in the server process and under the same policy as a read.

    Never the sandbox: this reads files, and only `run_bash` is ever confined (ADR-0010).
    """
    raw = _text_arg(args, "pattern")
    try:
        needle = re.compile(raw)
    except re.error as e:
        raise ToolRefused(
            f"{raw!r} is not a valid regular expression ({e}). Escape any literal "
            "brackets, braces or parentheses you meant as characters."
        ) from e

    name_glob = _text_arg(args, "glob") if "glob" in args else ""
    max_results = _int_arg(args, "max_results", 100)
    if max_results < 1:
        raise ToolRefused("max_results must be at least 1; a search for no results is not one.")

    if "path" in args:
        # `resolve_search_root`, not `_one_path`: the latter refuses a directory at layer 1
        # because a directory is not a thing to read, and a search scope is exactly that.
        # A file is accepted too, so pointing this at one narrows to it.
        scopes = (resolve_search_root(cfg, _text_arg(args, "path")),)
    else:
        # No path means the whole workspace, which is what makes this a search rather than
        # a second `read_file`. The roots arrive translated and symlink-resolved.
        scopes = resolved_roots(cfg)

    candidates: list[str] = []
    capped = False
    for scope in scopes:
        if os.path.isfile(scope):
            candidates.append(scope)
            continue
        more, hit = _search_candidates(cfg, scope, name_glob)
        candidates += more
        capped = capped or hit

    # One policy call for the whole batch, which is also what keeps `gitignored` to one
    # `check-ignore` per repository rather than one per file. Candidates the policy
    # declines are dropped rather than reported: nobody named them, so they are not
    # results -- see `resolve_permitted` on why that is a separate function.
    permitted = resolve_permitted(cfg, candidates, must_exist=True)
    hits = _search_hits(cfg, permitted, needle, max_results)

    if not hits.lines:
        where = str(args["path"]) if "path" in args else "the workspace"
        scope_note = f" among files matching {name_glob!r}" if name_glob else ""
        why = (
            "The scan cap was reached first, so this is not exhaustive -- narrow it with "
            "path or glob."
            if capped
            else "The pattern is absent from everything the path policy lets you read "
                 "there; it may still exist in a file that policy declines."
        )
        return f"No line matched {raw!r} in {where}{scope_note}. {why}"

    return _search_report(cfg, hits, capped=capped)


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

    entry = _one_path(cfg, given, must_exist=False)
    try:
        opened = open_resolved(entry, "wb")
        with opened.handle as fh:
            fh.write(encoded)
    except OSError as e:
        raise ToolRefused(f"could not write it: {e.strerror or e}") from e
    # Which verb comes from the open that made the file, not from a second `stat` on
    # the same string -- which was itself a second use of a path checked once.
    verb = "Created" if opened.created else "Overwrote"
    return f"{verb} {entry.posix} ({len(encoded)} bytes)."


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


def _run_bash(cfg: Config, args: dict[str, object], policy: BashPolicy) -> BashResult:
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
            # The workdir arrives already resolved and root-checked by
            # `paths.resolve_workdir`. It is not re-derived here: a second resolution is a
            # second chance to disagree with the first, and the check that matters happened
            # where the caller's argument was still a caller's argument.
            workdir=policy.workdir,
            network=policy.network,
            extra_binds=sandbox.probe_toolchain_binds(cfg) + policy.extra_binds,
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
            "Read a UTF-8 text file from the workspace, with every line numbered, so you "
            "can cite what you read as a file name and a line number. Paths must be "
            "absolute. Long files come back one range at a time: the result states the "
            "total number of lines and the line to continue from, so read the next range "
            "rather than the file again, and use start_line to go straight to the part you "
            "want. Refused for a path outside the workspace, an unlisted extension, a file "
            "git ignores, or anything that is not text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "start_line": {
                    "type": "integer",
                    "description": "First line to read, counting from 1. Omit to start at "
                                   "the beginning.",
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
    writes=True,
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
    wants_policy=True,
    writes=True,
)

SEARCH_FILES = RegisteredTool(
    spec=ToolSpec(
        name="search_files",
        description=(
            "Search the workspace for a regular expression and get back the matching "
            "lines, each as a file name, the word line, and a line number -- so you can "
            "read the part you want with read_file and cite it. This is how you find "
            "something whose location you do not know; read_file is for when you do. "
            "Omit path to search everywhere, or give a directory to narrow it, and use "
            "glob to restrict which file names are opened (a test helper is found far "
            "faster with glob=test_*.py than by reading directories). Files the path "
            "policy declines are not searched and are not reported: they are not results. "
            "The reply says when it stopped early or hit its scan cap -- read that before "
            "concluding something does not exist, because a narrowed search that found "
            "nothing is not proof of absence."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression, matched per line. Prefix "
                                   "(?i) to ignore case.",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path to a directory or file to search. Omit "
                                   "to search the whole workspace.",
                },
                "glob": {
                    "type": "string",
                    "description": "Only open files whose NAME matches this glob, e.g. "
                                   "*.py or test_*.py. Matches the name, not the path.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Most matching lines to return. Defaults to 100.",
                },
            },
            "required": ["pattern"],
        },
    ),
    handler=_search_files,
    cacheable=True,
)

# Insertion order is the declared order, and it is fixed here rather than sorted at use.
REGISTRY: dict[str, RegisteredTool] = {
    t.spec.name: t for t in (READ_FILE, SEARCH_FILES, WRITE_FILE, RUN_BASH)
}
ALL_TOOL_NAMES: frozenset[str] = frozenset(REGISTRY)

# Derived, never written out. This is the set `delegate_readonly` offers, and it is the
# whole content of ADR-0042's promise: not that the delegation has no tools, but that
# nothing it can do will write. A tool added without declaring `writes` lands in here,
# which is why the guard in tests/test_tools.py checks this against the registry rather
# than against a list somebody has to remember to edit.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    name for name, tool in REGISTRY.items() if not tool.writes
)

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
    the same bytes. Tool schemas sit in the cached prefix, so an order that varied per call
    would cost a fresh prefill for nothing.

    That was asserted here against ADR-0011, which fixes the prompt order but never says
    where a tool schema sits in it. Measured on 2026-09-02 and the assertion holds: sent
    cold, a request with one tool *description* reworded cached zero tokens, exactly like
    one with the system prompt reworded, where the unchanged prefix cached 4096. So editing
    a description below is a prefill bill as well as a contract change (JOURNAL 2026-09-02).
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
    cfg: Config,
    call: ToolUseBlock,
    allowed: Iterable[str],
    policy: BashPolicy | None = None,
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
        produced = (
            tool.handler(cfg, call.input, policy or BashPolicy())
            if tool.wants_policy
            else tool.handler(cfg, call.input)
        )
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
