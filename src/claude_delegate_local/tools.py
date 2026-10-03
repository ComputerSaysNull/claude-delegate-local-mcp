"""The tools the delegated model may call, and the two places `allowed_tools` is enforced.

Not `server.py`'s `@mcp.tool` functions, which *Claude* calls; these are what the local
model calls inside a delegation.

A fixed table, not a decorator mutating module state on import: tool schemas sit in the
cached prompt prefix, so the declared set must depend on its inputs alone, import order
included. Why: ADR-0011.

`allowed_tools` is checked twice, and the second time is the one that matters. Filtering the
declared list is advisory: a model can name a tool it was never offered, and some do. So
`declared_tools` decides what is *offered* and `execute_tool` what is *run*, from the same
set, and neither trusts the other. They sit together so a new tool cannot reach only one.

The resolved set is a parameter, never a config field: it is per agent or per call, and a
server-wide default would be a config default outside `config.py`.
"""

from __future__ import annotations

import bisect
import difflib
import fnmatch
import os
import posixpath
import re
import shutil
import subprocess
import threading
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256

from . import provision, sandbox
from .backends.base import BashOutcome, ToolResultBlock, ToolSpec, ToolUseBlock
from .config import Config
from .context import decode_text, line_number_width, numbered_line
from .paths import (
    PathPolicyError,
    PathRefused,
    ResolvedPath,
    extension_refusal,
    git_env,
    gitignored,
    hardened,
    key_material_marker,
    load_secret_globs,
    open_resolved,
    resolve_all,
    resolve_permitted,
    resolve_search_root,
    resolved_roots,
    secret_match,
    untrusted_git_config,
)


class ToolRefused(Exception):
    """A tool declined the call. Reported to the model, never raised past `execute_tool`."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """One tool: what the model is told, and what actually runs.

    `cacheable`: identical arguments must give the same answer within a delegation, which
    lets the loop serve a repeat. Declared here, since a loop deciding by name would keep a
    second copy of the fact. False is the safe default: a side-effecting tool is never
    cacheable and also invalidates the cache, since a file read before and after a write
    has two correct answers and serving the first hands the model a stale file.
    """

    spec: ToolSpec
    handler: Callable[..., str | BashResult]
    cacheable: bool = False
    # Whether the handler takes the `BashPolicy` as a third argument; declared here like
    # `cacheable`. Only `run_bash` enters the sandbox, and widening its signature beats an
    # ignored parameter on the others.
    wants_policy: bool = False
    # Whether the tool can change anything outside the server's memory.
    # `READ_ONLY_TOOL_NAMES` derives from it, which makes `delegate_readonly`'s readOnlyHint
    # a property of the tool rather than a claim; a hand-kept list would go stale silently
    # while the annotation was still advertised. False is NOT the safe default: every
    # writing tool sets it, and tests/test_tools.py asserts the derived set against the
    # registry.
    writes: bool = False
    # Whether the handler takes `SeenFiles` as a third argument. Only the file tools:
    # `read_file` records the bytes it read, `write_file` and `edit_file` the bytes they
    # wrote, and `edit_file` refuses a file whose bytes differ from that record. Declared
    # here like `wants_policy`, so `execute_tool` knows the signature without a second table.
    tracks_reads: bool = False


@dataclass(frozen=True, slots=True)
class BashPolicy:
    """What one delegation may do inside the sandbox: where, with what, and on whose network.

    Per delegation, never a config field, as with `allowed_tools`. The default is no
    workspace, no network, and only the toolchain binds the server probes itself: a caller
    who says nothing gets a sandbox that reaches nothing of theirs.
    """

    workdir: str | None = None
    network: bool = False
    extra_binds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BashResult:
    """A handler's text plus what the server measured, on the way to the result block.

    Only `run_bash` returns one, so one tool's contract widens, not all of them.
    """

    text: str
    outcome: BashOutcome
    is_error: bool = False


class SeenFiles:
    """What the model last saw on disk: resolved path -> sha256 of its bytes.

    Per delegation, created by the loop and passed to the file tools. `read_file` records
    the hash of the whole file's bytes it read; `write_file` and `edit_file` record the
    bytes they wrote; `edit_file` refuses when the bytes it is about to edit differ from
    the record, which means something other than this delegation's own edits changed the
    file since the model last saw it. `run_bash` is deliberately untouched: a file a
    command changed simply hashes differently the next time an edit tries it.

    Guarded by a lock because pooled `read_file` calls run on worker threads, so two reads
    of two files can touch the map at once. The hash is keyed by `entry.posix`, the one
    spelling `open_resolved` and the refusal both agree on.
    """

    __slots__ = ("_hash", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hash: dict[str, str] = {}

    def record(self, path: str, digest: str) -> None:
        """Remember `path` as last seen with `digest`, whatever the model just read or wrote."""
        with self._lock:
            self._hash[path] = digest

    def changed(self, path: str, digest: str) -> bool:
        """Whether `path` was seen before and now hashes differently.

        No record at all is False: an edit of a file the model never read behaves exactly
        as before, which is the safe default for the many callers with no `SeenFiles`.
        """
        with self._lock:
            seen = self._hash.get(path)
        return seen is not None and seen != digest


# --- argument helpers -------------------------------------------------------------------
#
# Checked, not trusted: a wrong type is a refusal the model can correct, not a traceback.


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


def _expected_count_arg(args: dict[str, object]) -> int | None:
    """`expected_count`, or None when omitted; below 1 or non-integer is refused."""
    value = args.get("expected_count")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolRefused("'expected_count' must be a whole number, not "
                          f"{type(value).__name__}.")
    if value < 1:
        raise ToolRefused("'expected_count' must be at least 1.")
    return value


def _one_path(
    cfg: Config,
    given: str,
    *,
    must_exist: bool,
    writing: bool = False,
    surface: str = "`path` argument",
) -> ResolvedPath:
    """Run the caller's path through the four-layer policy and return the resolved entry.

    `paths.py` governs both file tools and the sandbox governs none of them: only `run_bash`
    is ever confined, so this is the whole control for a read or a write. Why: ADR-0010.

    The entry, not its `.posix`. Handing back a string lets a caller check a name and then
    open a file, which are the same thing only for as long as nothing changes in between;
    `open_resolved` is the only sanctioned way to open what this returns. Why: ADR-0049.
    """
    # A tool argument, never `files[]`: this runs inside a tool call, so the refusal goes
    # back to the model as an error result and the delegation carries on. Naming the
    # prefetch argument would send the model to correct something it did not write.
    resolved = resolve_all(
        cfg, [given], must_exist=must_exist, writing=writing, surface=surface,
        before_dispatch=False,
    )
    if not resolved:
        # Layer 4 dropped it without a refusal only if the caller passed nothing at all.
        raise ToolRefused(f"{given!r} resolved to no file.")
    return resolved[0]


# --- the tools --------------------------------------------------------------------------


def _read_file(cfg: Config, args: dict[str, object], seen: SeenFiles | None = None) -> str:
    entry = _one_path(cfg, _text_arg(args, "path"), must_exist=True)
    # 1-based, like an editor. Numbered so the model can cite what it read.
    start = _int_arg(args, "start_line", 1)
    if start < 1:
        raise ToolRefused(
            f"start_line {start} is not a line number; lines are counted from 1.")
    # 0 means "no end given"; it cannot collide with a real line, which starts at 1.
    end = _int_arg(args, "end_line", 0)
    if end and end < start:
        raise ToolRefused(
            f"end_line {end} is before start_line {start}, so the range is empty. "
            "Returning nothing would read as a fact about the file rather than about "
            "the request."
        )

    # Sized from the descriptor: the ceiling refuses a huge file without reading it, and
    # measures the file actually held.
    try:
        opened = open_resolved(entry, "rb", scan_bytes=cfg.secret_content_scan_bytes)
    except OSError as e:
        raise ToolRefused(f"could not read it: {e.strerror or e}") from e

    try:
        with opened.handle as fh:
            nbytes = os.fstat(fh.fileno()).st_size
            if nbytes > cfg.max_file_read_bytes:
                # Refused, not paged: paging it would outlast the turn budget.
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

    # `splitlines()`: `split("\n")` invents a trailing empty line. Endings re-join as "\n",
    # so a CRLF file reads the same.
    lines = text.splitlines()
    total = len(lines)
    if start > total and total:
        raise ToolRefused(
            f"start_line {start} is past the end; the file has {total} lines.")

    # Bounded by `max_read_chars` too, on a line boundary, since a half line's number would
    # lie. `end_line` past the end means the end; `start_line` past it was refused, since
    # starting there asks for nothing.
    last = min(end, total) if end else total

    out: list[str] = []
    width = line_number_width(total)
    used = 0
    index = start
    for index in range(start, last + 1):
        rendered = numbered_line(index, lines[index - 1], width)
        if out and used + len(rendered) + 1 > cfg.max_read_chars:
            break
        out.append(rendered)
        used += len(rendered) + 1
    else:
        # No break, so `index` is the last line, not the next.
        index = last + 1

    # `raw` is the whole file: the size was checked against the ceiling before the read, so
    # the model last saw exactly these bytes. Any range records the whole file, since the
    # range is only a rendering choice and the content the model may quote is the file's.
    if seen is not None:
        seen.record(entry.posix, sha256(raw).hexdigest())

    body = "\n".join(out)
    # Against `last`: a range ending where asked is complete, not "truncated".
    if index > last:
        return body
    # The next `start_line`, so a continuation is a range, not a whole re-read. In the
    # result, never the prompt, which must stay byte-identical (ADR-0011).
    return (
        f"{body}\n\n[truncated: lines {start} to {index - 1} of {total}. "
        f"Call read_file again with start_line={index} for the rest.]"
    )


# The `path` meaning "every workspace root". A sentinel, since an omission looks like a
# model that decided nothing, and a deliberate full walk becomes one greppable string. It
# cannot collide with a real `path`, because layer 1 refuses anything not absolute.
UNSCOPED = "_unscoped_"

# Entries listed per root before the rest are summarised, so the map stays glanceable in a
# cached prefix.
LAYOUT_MAX_ENTRIES = 40


def _top_level(root: str, globs: Sequence[str]) -> tuple[str, ...]:
    """What is directly inside `root`: directories marked with a trailing slash, then files.

    One level: at declaration time a recursive listing would be the very walk the map
    avoids. **Files too**, or a directory of only files (the repository root) would look
    empty and be ruled out. Filtered as the walk prunes: symlinks skipped so no tree has two
    names, and denylisted entries not named, since the model would scope a search there and
    read the walk's empty result as absence rather than policy.
    """
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        # The operator's problem: the listing degrades rather than failing declaration.
        return ()
    dirs: list[str] = []
    files: list[str] = []
    for name in entries:
        full = posixpath.join(root, name)
        if os.path.islink(full) or secret_match(full, globs) is not None:
            continue
        if os.path.isdir(full):
            dirs.append(name + "/")
        elif os.path.isfile(full):
            files.append(name)
    # Directories first, being what a search scopes to; files show it is not empty.
    return tuple(dirs + files)


def _render_entries(inside: Sequence[str]) -> str:
    """One root's listing, capped, saying how many it did not name.

    "+312 more" says the directory is dense and worth scoping into; silent truncation
    would suggest it had been shown everything.
    """
    if not inside:
        return "(empty)"
    if len(inside) <= LAYOUT_MAX_ENTRIES:
        return ", ".join(inside)
    shown = ", ".join(inside[:LAYOUT_MAX_ENTRIES])
    return f"{shown}, +{len(inside) - LAYOUT_MAX_ENTRIES} more"


def _workspace_layout(cfg: Config) -> str:
    """Every workspace root and what is directly inside it, as one block of text.

    Told, not urged: a model given only root names cannot scope (ADR-0076). The denylist is
    read once, hence `_top_level` takes globs. Deterministic for a config, since tool
    schemas sit in the cached prefix (ADR-0011); a new top-level entry costs one cold
    prefill.
    """
    globs = load_secret_globs(cfg)
    roots = resolved_roots(cfg)
    if not roots:
        return "No workspace roots are configured, so there is nothing to search."
    lines = [f"  {root} -> {_render_entries(_top_level(root, globs))}" for root in roots]
    return "Workspace roots, and what is directly inside each one:\n" + "\n".join(lines)


def _scope_help(cfg: Config) -> str:
    """The map plus what to do with it. One string, used in two homes.

    One string so the two homes cannot drift. The refusal fires when the model has just
    shown it does not know the layout: the moment for the whole map.
    """
    return (
        _workspace_layout(cfg)
        + f"\nA name ending in `/` is a directory. Give `path` one of them, or something "
          f"deeper -- naming the root on the left of the arrow walks all of it, which is "
          f"slower than you usually want. Pass the exact string {UNSCOPED!r} when you "
          "genuinely need every root walked."
    )


def _root_scope_note(cfg: Config, root: str) -> str:
    """What to say when a search scopes itself to a whole workspace root.

    A note, not a refusal (ADR-0087), naming the root's children: the model has shown it
    knows the root and not what is under it. No speed multiple is quoted, being this
    hardware's (ADR-0074).
    """
    inside = _render_entries(_top_level(root, load_secret_globs(cfg)))
    return (
        f"Walked all of {root}, which is a whole workspace root. Scoping to one of these "
        f"is faster, and so is anything deeper: {inside}. Pass the exact string "
        f"{UNSCOPED!r} when you do need every root walked."
    )


def _search_candidates(cfg: Config, scope: str, name_glob: str) -> tuple[list[str], bool]:
    """Enumerate files under `scope` that are worth asking the policy about.

    Enumeration only: whether a file may be *read* is `resolve_permitted`'s, over the four
    layers. This keeps the policy from a hundred thousand candidates with cheap string
    tests, and pruning denylisted directories keeps the walk out of `.git`.

    Gitignored directories are pruned for the scan cap's sake: spent inside them, it stops
    the walk before the source. One batch per level, with `tops` carrying the work-tree
    lookup down. This does not make `resolve_permitted`'s own `gitignored` call redundant:
    an ignored file in a directory that is not is only caught there.

    Returns the candidates and whether the scan cap was reached.
    """
    globs = load_secret_globs(cfg)
    found: list[str] = []
    scanned = 0
    capped = False
    tops: dict[str, str | None] = {}

    for dirpath, dirnames, filenames in os.walk(scope):
        # Pruned in place; symlinks skipped so no tree is enumerated under two names.
        kept = []
        for name in sorted(dirnames):
            full = posixpath.join(dirpath, name)
            if os.path.islink(full) or secret_match(full, globs) is not None:
                continue
            kept.append(name)

        if cfg.respect_gitignore and kept:
            # One `check-ignore` for the level, since the walk descends into every survivor.
            ignored = gitignored([posixpath.join(dirpath, n) for n in kept], tops=tops)
            kept = [n for n in kept if posixpath.join(dirpath, n) not in ignored]

        dirnames[:] = kept

        for name in sorted(filenames):
            if scanned >= cfg.search_max_files_scanned:
                capped = True
                return found, capped
            full = posixpath.join(dirpath, name)
            if os.path.islink(full):
                continue
            # Layer 2's own predicate, since `Makefile` is allowlisted by whole name. The
            # full path, so a repository's own `ext_allowlist_extra` can widen it.
            if extension_refusal(cfg, full) is not None:
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

    By `max_results`, as asked, and `max_read_chars`, or a pattern matching every line
    would return the file.
    """
    lines: list[str] = []
    files = 0
    used = 0
    truncated = False

    for item in paths:
        if len(lines) >= max_results:
            return _Hits(tuple(lines), files, True)
        try:
            with open_resolved(
                item, "rb", scan_bytes=cfg.secret_content_scan_bytes
            ).handle as fh:
                blob = fh.read(cfg.max_file_read_bytes)
        except (OSError, PathRefused):
            # Changed since the walk: dropped, not refused, since nobody named this path.
            continue
        text, _ = decode_text(blob)
        if text is None:
            continue

        hits = 0
        for number, line in enumerate(text.splitlines(), start=1):
            if not needle.search(line):
                continue
            # Never "<file>:<n>": with four digits it reads as a host and port, which this
            # project refuses wherever text can reach a commit or pull request.
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


def _unscoped_note(cfg: Config) -> str:
    """What an omitted `path` actually cost, said where the model will read it.

    In the result, read every time, not the schema, read once; refusing instead would break
    a legitimate search such as every `conftest.py`. Names the roots, not a speed multiple,
    which is this hardware's (ADR-0074).
    """
    return (
        "Scanned every workspace root, because `path` was omitted: "
        + ", ".join(resolved_roots(cfg))
        + ". Naming a directory in `path` is far faster -- a `glob` does not narrow this, "
          "it only narrows which of the walked files are opened."
    )


def _search_report(
    cfg: Config, hits: _Hits, *, capped: bool, unscoped: bool, root_scope: str | None = None
) -> str:
    """The result, with every reason it might be incomplete stated in it.

    A truncated search that reads as exhaustive makes the model confidently deny a symbol
    exists. A whole-root walk gets its answer, with the faster-scope note alongside.
    """
    tail: list[str] = [f"{len(hits.lines)} matching line(s) in {hits.files} file(s)."]
    if unscoped:
        tail.append(_unscoped_note(cfg))
    if root_scope is not None:
        tail.append(_root_scope_note(cfg, root_scope))
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

    # Required, and refused with the map: a model omits it for lack of any directory name.
    # Why: ADR-0076.
    given = _text_arg(args, "path", required=False)
    if not given:
        raise ToolRefused(
            "`path` is required: name the directory to search. " + _scope_help(cfg)
        )

    root_scope: str | None = None
    if given == UNSCOPED:
        # The deliberate escape. Checked before layer 1, which refuses it for not being
        # absolute, which is why no real path collides with it.
        scopes = resolved_roots(cfg)
    else:
        # Not `_one_path`, which refuses a directory as unreadable; a file also works.
        scope = resolve_search_root(
            cfg, given, surface="`path` argument", before_dispatch=False,
        )
        # Compared resolved: a trailing slash, `.` or a symlink all name the same root.
        # Why: ADR-0087.
        if scope in resolved_roots(cfg):
            root_scope = scope
        scopes = (scope,)

    candidates: list[str] = []
    capped = False
    for scope in scopes:
        if os.path.isfile(scope):
            candidates.append(scope)
            continue
        more, hit = _search_candidates(cfg, scope, name_glob)
        candidates += more
        capped = capped or hit

    # One policy call for the batch, so one `check-ignore` per repository. Declined
    # candidates are dropped, not reported: nobody named them (see `resolve_permitted`).
    permitted = resolve_permitted(cfg, candidates, must_exist=True)
    hits = _search_hits(cfg, permitted, needle, max_results)

    if not hits.lines:
        where = "every workspace root" if given == UNSCOPED else given
        scope_note = f" among files matching {name_glob!r}" if name_glob else ""
        why = (
            "The scan cap was reached first, so this is not exhaustive -- narrow it with "
            "path or glob."
            if capped
            else "The pattern is absent from everything the path policy lets you read "
                 "there; it may still exist in a file that policy declines."
        )
        note = f" {_unscoped_note(cfg)}" if given == UNSCOPED else ""
        if root_scope is not None:
            note = f" {_root_scope_note(cfg, root_scope)}"
        return f"No line matched {raw!r} in {where}{scope_note}. {why}{note}"

    return _search_report(
        cfg, hits, capped=capped, unscoped=given == UNSCOPED, root_scope=root_scope
    )


def _write_file(cfg: Config, args: dict[str, object], seen: SeenFiles | None = None) -> str:
    given = _text_arg(args, "path")
    content = _text_arg(args, "content")
    encoded = content.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        # Refused, not truncated: a silently shortened file is a corrupt one.
        raise ToolRefused(
            f"{len(encoded)} bytes exceeds the {cfg.max_write_bytes}-byte limit for one "
            f"write. Write it in parts.")

    entry = _one_path(cfg, given, must_exist=False, writing=True)
    try:
        opened = open_resolved(entry, "wb")
        with opened.handle as fh:
            fh.write(encoded)
    except OSError as e:
        raise ToolRefused(f"could not write it: {e.strerror or e}") from e
    # The bytes written are what the model last saw, so a later edit of this file is not
    # a change. Recorded even though `write_file` never refuses: the record exists so the
    # edit that follows can be judged against it.
    if seen is not None:
        seen.record(entry.posix, sha256(encoded).hexdigest())
    # From the open itself, since a second `stat` would reuse a path checked once.
    verb = "Created" if opened.created else "Overwrote"
    return f"{verb} {entry.posix} ({len(encoded)} bytes)."


# The control characters an edit most often gets wrong, which `unicodedata` leaves unnamed.
_CONTROL_NAMES = {"\t": "TAB", "\n": "LINE FEED", "\r": "CARRIAGE RETURN"}


def _code_point(ch: str) -> str:
    """One character as `U+XXXX NAME`, or `U+XXXX` when it has no name."""
    name = _CONTROL_NAMES.get(ch) or unicodedata.name(ch, "")
    return f"U+{ord(ch):04X} {name}".rstrip()


def _line_starts(text: str) -> list[int]:
    """The character offset at which each line begins, so a column is cheap to recover."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_col(starts: Sequence[int], index: int) -> tuple[int, int]:
    """The 1-based line and column for a character offset."""
    line = bisect.bisect_right(starts, index)
    return line, index - starts[line - 1] + 1


def _nearest_miss(text: str, old: str) -> str | None:
    """Where `old` almost is, as a line/column hint, or None when it is not close.

    "Read it again" alone gets the same edit resent, so this names the region and first
    differing character. Never file text, since the refusal lands in the operator
    transcript: only lines, a column and code points. Why: ADR-0039.
    """
    old_lines = old.splitlines()
    head_index = next((i for i, line in enumerate(old_lines) if line.strip()), None)
    if head_index is None:
        return None
    head = old_lines[head_index]
    file_lines = text.splitlines()
    stripped = [line.strip() for line in file_lines]
    target = head.strip()
    if target in stripped:
        candidates = [i for i, s in enumerate(stripped) if s == target]
    else:
        matches = difflib.get_close_matches(head, file_lines, n=1, cutoff=0.6)
        if not matches:
            return None
        match = matches[0]
        candidates = [i for i, line in enumerate(file_lines) if line == match]
    if not candidates:
        return None

    starts = _line_starts(text)
    old_head_offset = sum(len(line) + 1 for line in old_lines[:head_index])
    a = old[old_head_offset:]
    best_cand = candidates[0]
    best_i = -1
    for cand in candidates:
        # In place: a line like `}` has hundreds of candidates, each a whole-file slice.
        origin = starts[cand]
        limit = min(len(a), len(text) - origin)
        i = 0
        while i < limit and a[i] == text[origin + i]:
            i += 1
        if i > best_i:
            best_i = i
            best_cand = cand

    start = best_cand + 1
    b = text[starts[best_cand]:]
    if best_i >= len(b):
        # The file ends first, so only the caller's character can be named.
        return (
            f"The closest match starts at line {start}; it first differs where the file "
            f"ends, and you have {_code_point(a[best_i])}. Read from line {start} and "
            f"quote it exactly."
        )
    if best_i >= len(a):
        # The caller's text runs out first, the mirror of the case above.
        return (
            f"The closest match starts at line {start}; it first differs where your text "
            f"ends, and the file has {_code_point(b[best_i])}. Read from line {start} "
            f"and quote it exactly."
        )
    line, col = _line_col(starts, starts[best_cand] + best_i)
    return (
        f"The closest match starts at line {start}; it first differs at line {line}, "
        f"column {col}, where the file has {_code_point(b[best_i])} and you have "
        f"{_code_point(a[best_i])}. Read from line {start} and quote it exactly."
    )


# A quoted line that carries read_file's line number and its tab separator.
_PREFIX_NUMBERED = re.compile(r"^ *\d+\t")


def _quoted_prefix_hint(text: str, old: str) -> str | None:
    """Where `old` quotes `read_file`'s line-number prefix back at the file.

    `read_file` prefixes every line with its number and a tab. A model copying that prefix
    into `old_string` over-indents the quote and misses, and "read it again" alone gets the
    same edit resent. Name what the prefix is so the model drops it. Never file text, since
    the refusal lands in the operator transcript: only a description. Why: ADR-0039.
    """
    # The whole quote, prefix removed, must be in the file -- a line at a time, a short
    # line like `)` would match anywhere and the hint would name a cause that is not one.
    lines = old.split("\n")
    body = [line for line in lines if line.strip()]
    if not body:
        return None

    if all(_PREFIX_NUMBERED.match(line) for line in body):
        bare = "\n".join(_PREFIX_NUMBERED.sub("", line, count=1) for line in lines)
        if bare in text:
            return (
                "The quote includes read_file's line numbers and the tab after them, which "
                "are not file content. Quote only what follows the tab."
            )

    if all(line.startswith("\t") for line in body):
        bare = "\n".join(line.removeprefix("\t") for line in lines)
        if bare in text:
            return (
                "Each quoted line starts with a tab the file does not have there; that "
                "tab is read_file's separator after the line number. Drop it and quote "
                "the line as it appears in the file."
            )

    return None


def _edit_plan(
    text: str, old: str, new: str, expected: int | None
) -> tuple[bytes, int]:
    """Count `old` in `text`, validate against `expected`, and build the replacement.

    Returns the encoded replacement and how many occurrences it covers. A missing or
    ambiguous match is a refusal: stating `expected_count` is the model's own check
    that it is editing what it thinks.
    """
    found = text.count(old)
    if found == 0:
        message = (
            "'old_string' does not appear in the file, so nothing was written. Read "
            "the file again -- what you quoted is either stale or not exactly what is "
            "there, and whitespace counts."
        )
        hint = _quoted_prefix_hint(text, old) or _nearest_miss(text, old)
        if hint:
            message = f"{message} {hint}"
        raise ToolRefused(message)
    if expected is not None:
        if found != expected:
            raise ToolRefused(
                f"'old_string' appears {found} times, but expected_count is "
                f"{expected}, so nothing was written."
            )
        return text.replace(old, new).encode("utf-8"), found
    if found > 1:
        raise ToolRefused(
            f"'old_string' appears {found} times, so which one to replace is "
            f"ambiguous and nothing was written. Quote more of the surrounding "
            f"lines until the text appears exactly once."
        )
    return text.replace(old, new, 1).encode("utf-8"), found


def _edit_file(cfg: Config, args: dict[str, object], seen: SeenFiles | None = None) -> str:
    """Replace one exact occurrence of `old_string`, or exactly `expected_count` of them,
    or refuse and change nothing.

    A count rather than a replace-all flag, so a model replacing a repeat on purpose still
    states what it believes is there, and a wrong belief is refused like a wrong quote.

    Not `write_file`, where rewriting a long module whole lands any hallucinated character
    silently where nobody looks.

    **Refusing an ambiguous or absent match is the feature.** A stale quote matches nothing
    and a common one matches twice; both mean the model is not editing what it thinks, and
    saying so costs a turn where guessing costs an unnoticed corrupt file. Hence text, not
    line numbers: a stale number overwrites the wrong region with no way to tell.

    One descriptor for the whole read-modify-write, held by `open_resolved`; two opens
    against one check is the gap ADR-0049 closes.
    """
    entry = _one_path(cfg, _text_arg(args, "path"), must_exist=True, writing=True)
    old = _text_arg(args, "old_string")
    new = _text_arg(args, "new_string")
    if not old:
        raise ToolRefused(
            "'old_string' is empty, which matches everywhere and identifies nothing. Quote "
            "the text to replace, including enough of its surroundings to be unique."
        )
    if old == new:
        raise ToolRefused(
            "'old_string' and 'new_string' are identical, so this call would change "
            "nothing. Nothing was written."
        )

    expected = _expected_count_arg(args)

    try:
        opened = open_resolved(entry, "r+b", scan_bytes=cfg.secret_content_scan_bytes)
    except OSError as e:
        raise ToolRefused(f"could not read it: {e.strerror or e}") from e

    with opened.handle as fh:
        try:
            nbytes = os.fstat(fh.fileno()).st_size
            if nbytes > cfg.max_file_read_bytes:
                raise ToolRefused(
                    f"it is {nbytes} bytes, over the {cfg.max_file_read_bytes}-byte "
                    f"ceiling, so it was not read at all and nothing was written. Use "
                    f"run_bash with sed to edit a file this size."
                )
            raw = fh.read()
        except OSError as e:
            raise ToolRefused(f"could not read it: {e.strerror or e}") from e

        text, why = decode_text(raw)
        if text is None:
            raise ToolRefused(why)

        # The bytes we are about to edit are hashed from this same descriptor's read, never
        # a second open, so nothing can swap the file between the check and the write. A
        # record that disagrees means the file changed since the model last saw it -- a
        # `run_bash` command, the operator -- and a still-matching quote is exactly the case
        # worth catching, so it is refused. Never file text in the message: it lands in the
        # operator transcript.
        if seen is not None and seen.changed(entry.posix, sha256(raw).hexdigest()):
            raise ToolRefused(
                "this file changed since it was last read, not by an edit this delegation "
                "made, so the edit was refused. Read the file again before editing; "
                "nothing was written."
            )

        edited, count = _edit_plan(text, old, new, expected)
        if len(edited) > cfg.max_write_bytes:
            # `write_file`'s limit, refused for the same reason.
            raise ToolRefused(
                f"the result would be {len(edited)} bytes, over the "
                f"{cfg.max_write_bytes}-byte limit for one write. Nothing was written."
            )

        try:
            fh.seek(0)
            fh.write(edited)
            # Truncated after writing, not before, so every refusal above leaves the file
            # intact and the model retries rather than repairs.
            fh.truncate()
        except OSError as e:
            raise ToolRefused(f"could not write it: {e.strerror or e}") from e

    # The bytes now on disk are what the model last saw, so the next edit is not a change.
    if seen is not None:
        seen.record(entry.posix, sha256(edited).hexdigest())

    word = "occurrence" if count == 1 else "occurrences"
    return f"Replaced {count} {word} in {entry.posix} ({len(edited)} bytes)."


def _capped(cfg: Config, result: sandbox.SandboxResult) -> str:
    """stdout and stderr, labelled, cut to the cap with the true length stated.

    Labelled, since printing nothing and printing to stderr are different facts. Cut, not
    refused like a write, because a truncated log is still evidence; the tail is kept, as a
    build says what went wrong last.
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


def _protected_note(moved: Sequence[str]) -> str:
    """What the model is told when its command created a file a host program acts on."""
    if not moved:
        return ""
    return (
        f"\n\nThis command created {', '.join(moved)}, which a host program acts on as "
        f"configuration. Each was moved aside to a `.delegate-refused` name rather than "
        f"left in effect. Report the change you meant to make instead."
    )


def stale_env_note(cfg: Config, workdir: str | None) -> str | None:
    """One sentence saying `$DELEGATE_PYTHON` is unset because the environment is stale.

    A stale environment otherwise looks exactly like one never provisioned.
    """
    if provision.stale_for(cfg, workdir) is None:
        return None
    return (
        "$DELEGATE_PYTHON is unset because the environment provisioned for this project is "
        "stale: its dependency declaration changed after it was built, and nothing here can "
        "rebuild it -- say so rather than testing without it."
    )


# Beside a failed command in a shell with nothing of the caller's bound. The file tools
# resolve against the workspace roots, so a model can write a file its shell then cannot
# find, and "No such file" alone sends it probing a path that exists.
NO_WORKDIR_NOTE = (
    "No workdir is bound, so this shell sees none of your files; write_file and edit_file "
    "do. If the command needed them, say so rather than retrying: a workdir is the "
    "caller's to give."
)


def _run_bash(cfg: Config, args: dict[str, object], policy: BashPolicy) -> BashResult:
    """Run one command in the sandbox, and report what the server saw it do.

    Every path that did not reach a real process exit reports `exit_code=None`, and `ran`
    separates "nothing ran" from "it ran and said nothing": the model's account is not
    evidence, and a refusal must not be able to invent a 0. Why: ADR-0007, ADR-0010.

    Refusals return rather than raise, so the ledger counts the attempt, as `tool_calls`
    beside it already does.
    """
    try:
        command = _text_arg(args, "command")
    except ToolRefused as e:
        return BashResult(str(e), BashOutcome(exit_code=None), is_error=True)

    try:
        result = sandbox.run(cfg, sandbox.SandboxRequest(
            command=command,
            home=sandbox.resolve_home(cfg),
            # Already resolved and root-checked by `paths.resolve_workdir`, and not
            # re-derived: a second resolution is a second chance to disagree with the check
            # that ran on the caller's own argument.
            workdir=policy.workdir,
            network=policy.network,
            extra_binds=sandbox.probe_toolchain_binds(cfg) + policy.extra_binds,
            # The provisioned interpreter goes as an environment name, not a wider
            # `SANDBOX_PATH` or a rewritten command: rewriting means parsing shell, and one
            # project's tools on every PATH is how a command silently gets the wrong python.
            # The `--setenv` lands in the recorded argv. Absent unless something *current*
            # is provisioned, since a stale one would give exit codes that lie.
            env=sandbox.scratch_cache_env({
                **sandbox.resolve_env(cfg),
                **provision.sandbox_env(cfg, policy.workdir),
            }),
        ))
    except (sandbox.SandboxUnavailable, sandbox.SecretShadowIncomplete) as e:
        return BashResult(str(e), BashOutcome(exit_code=None), is_error=True)

    moved = _protected_note(result.protected_moved)
    if result.timed_out:
        # Said in words, not left to a null exit code. "No exit code" must not be readable
        # as success by a model summarising its own run.
        return BashResult(
            f"Timed out after {cfg.run_bash_timeout}s and was killed.{moved} Output up to "
            f"that point:\n\n{_capped(cfg, result)}",
            BashOutcome(exit_code=None, timed_out=True, ran=True, stages=result.stages),
            is_error=True,
        )

    body = _capped(cfg, result)
    # Told to the model too, which can re-run the failed step; counting it silently would
    # hide what makes the printed exit code misleading. Not an error: the line did as asked.
    masked = (
        "\n\nA command in this line exited non-zero before the last one. The exit status "
        "above is the last command's, so it does not report that."
        if result.masked_failure
        else ""
    )
    failed = result.exit_code != 0 or result.masked_failure
    unbound = NO_WORKDIR_NOTE if policy.workdir is None and failed else None
    note_part = "".join(
        f"\n\n{n}" for n in (stale_env_note(cfg, policy.workdir), unbound) if n
    )
    return BashResult(
        f"exit {result.exit_code}{masked}{moved}\n\n{body}{note_part}",
        BashOutcome(
            exit_code=result.exit_code, ran=True, masked_failure=result.masked_failure,
            stages=result.stages,
        ),
        is_error=result.exit_code != 0,
    )


# The git subcommands this server will run, each with the flags it accepts. Denied by
# default in both directions: a subcommand absent here, and a flag absent from its set, are
# refused. A constant, not config: which subcommands cannot write is a fact about git, and
# an operator who could widen it could turn a read-only tool into `git push`.
#
# Every flag was checked for whether it can write a file or run a program; the ones
# deliberately absent are why this is an allowlist, not a filter. `git log --output=<path>`
# really creates that file, and `--ext-diff` hands the diff to an external command.
#
# Git-level options (`-c`, `--exec-path`, `--upload-pack`) are unreachable by the argv
# shape: every model argument lands *after* the subcommand, and `git log -c` parses as
# `--cc`. They stay refused anyway, since a table listing only what is exploitable now
# must be re-audited whenever the argv changes.
GIT_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "log": frozenset({
        "--oneline", "--stat", "--shortstat", "--numstat", "--name-only", "--name-status",
        "--max-count", "-n", "--skip", "--since", "--until", "--after", "--before",
        "--author", "--committer", "--grep", "--no-merges", "--merges", "--first-parent",
        "--reverse", "--follow", "--all", "--date", "--format", "--pretty", "--graph",
        "--decorate", "--abbrev-commit", "-i", "--regexp-ignore-case",
    }),
    "show": frozenset({
        "--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--oneline",
        "-s", "--no-patch", "--format", "--pretty", "--date", "--abbrev-commit",
    }),
    "diff": frozenset({
        "--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--cached",
        "--staged", "-U", "--unified", "-w", "--ignore-all-space", "--find-renames",
        "-M", "--no-color",
    }),
    "blame": frozenset({"-L", "-w", "--porcelain", "--line-porcelain", "-s", "-e"}),
    "rev-parse": frozenset({
        "--abbrev-ref", "--short", "--verify", "--is-inside-work-tree", "--show-toplevel",
        "--git-dir", "HEAD",
    }),
    "ls-files": frozenset({
        "--cached", "--others", "--modified", "--deleted", "--exclude-standard", "--stage",
    }),
    "shortlog": frozenset({"-s", "-n", "-e", "--no-merges", "--summary", "--numbered"}),
    # `rev-list --count` is how you count commits, and `log` piped to nothing cannot.
    # Read-only like the rest.
    "rev-list": frozenset({
        "--count", "--all", "--max-count", "-n", "--skip", "--no-merges", "--merges",
        "--first-parent", "--reverse", "--since", "--until", "--after", "--before",
        "--author", "--committer", "--grep",
    }),
    "status": frozenset({"--short", "--porcelain", "--branch", "--untracked-files"}),
}

# Where a bare `-5` means "five commits": `git log -1` is idiomatic, and refusing it costs
# a turn. Not everywhere, since `git shortlog -n` means `--numbered`.
GIT_COUNT_SHORTHAND = frozenset({"log", "rev-list"})

# A long `git log` holds the server process, not a sandbox; generous enough for a real
# repository's whole log, and a constant because it bounds this tool, not a deployment.
GIT_TIMEOUT_SECONDS = 60.0

# Subcommands that render a diff, and so would run a textconv or external diff driver.
# Belt and braces: `untrusted_git_config` refuses a repository whose own config names one,
# and these keep the operator's global drivers out of what the model reads.
GIT_NO_TEXTCONV = frozenset({"log", "show", "diff", "blame"})
GIT_NO_EXT_DIFF = frozenset({"log", "show", "diff"})


def git_available() -> bool:
    """Whether this host has git at all. The same shape as `sandbox.available`."""
    return shutil.which("git") is not None


def _run_git(argv: list[str]) -> tuple[int, str, str]:
    """One git invocation. Fixed argv, never a shell, never a pager."""
    try:
        # Fixed argv from the allowlist above, and no shell: nothing the model wrote
        # reaches a command interpreter. Hardened and filtered exactly as layer 4's own
        # git is -- `paths.GIT_HARDENING` says why.
        done = subprocess.run(
            hardened(argv),
            env=git_env(),
            capture_output=True,
            # Closed: some subcommands (`shortlog`) read a non-terminal stdin, giving a
            # wrong answer or a 60-second hang inside the server.
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as e:
        raise ToolRefused("git is not installed on this host.") from e
    except subprocess.TimeoutExpired as e:
        raise ToolRefused(
            f"git did not finish within {GIT_TIMEOUT_SECONDS:.0f}s and was stopped. Narrow "
            f"it -- a revision range, a path, or a smaller max_count."
        ) from e
    out, _ = decode_text(done.stdout)
    err, _ = decode_text(done.stderr)
    return done.returncode, out or "", err or ""


# Short flags whose value may be attached (`-U1`, `-M50%`, `-n5`, `-L1,5`); for `-U` and
# `-M` it is the only spelling, since `diff -U 1` reads `1` as a revision.
GIT_SHORT_VALUE_FLAGS = frozenset({"-U", "-M", "-n", "-L"})


def _git_flag(token: str) -> str:
    """The flag's name, without any attached value. `--since=yesterday` is `--since`,
    and `-U1` is `-U`."""
    if not token.startswith("--") and len(token) > 2 and token[:2] in GIT_SHORT_VALUE_FLAGS:
        return token[:2]
    return token.split("=", 1)[0]


def _checked_args(command: str, raw: object) -> list[str]:
    """Every argument, refused unless the allowlist admits it.

    Flags are checked by name against this subcommand's own set. A non-flag token is a
    revision and is passed through -- git cannot be made to leave the repository by one,
    and the repository is what was validated.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        raise ToolRefused("'args' must be a list of strings.")
    permitted = GIT_SUBCOMMANDS[command]
    out: list[str] = []
    for token in raw:
        if token.startswith("-"):
            name = _git_flag(token)
            if (
                command in GIT_COUNT_SHORTHAND
                and len(token) > 1
                and token[1:].isdigit()
            ):
                out.append(token)
                continue
            if name not in permitted:
                raise ToolRefused(
                    f"{name!r} is not an accepted flag for 'git {command}' here. Accepted: "
                    f"{', '.join(sorted(permitted))}. The tool runs a fixed argv with no "
                    f"shell, and flags that can write a file or run a program are refused "
                    f"rather than filtered."
                )
        out.append(token)
    return out


def _split_separator(args: object, paths: object) -> tuple[object, object]:
    """`args` up to a `--`, and `paths` with whatever followed it added.

    Git's own convention and a model's first reach; what follows `--` joins `paths` and is
    checked as they are. A non-list passes through for the checks below to refuse.
    """
    if not isinstance(args, list) or "--" not in args:
        return args, paths
    at = args.index("--")
    if paths is not None and not isinstance(paths, list):
        return args[:at], paths
    return args[:at], [*(paths or []), *args[at + 1:]]


def _checked_paths(raw: object) -> list[str]:
    """Repo-relative path arguments, or a refusal.

    **Not** through `paths.py`, which validates a path that exists *now*, where history is
    about deleted and renamed files. So these are checked for what matters here, that they
    cannot address anything outside the repository, with git's own refusal as second net.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ToolRefused("'paths' must be a list of strings.")
    out: list[str] = []
    for given in raw:
        if not given:
            raise ToolRefused("An empty string is not a path.")
        if posixpath.isabs(given) or (len(given) > 1 and given[1] == ":"):
            raise ToolRefused(
                f"{given!r} is absolute; 'paths' are relative to the repository root, "
                f"because a revision's file may not exist on disk at all."
            )
        parts = given.replace("\\", "/").split("/")
        if ".." in parts:
            raise ToolRefused(
                f"{given!r} climbs out of the repository. Paths are relative to its root."
            )
        out.append(given)
    return out

# Subcommands whose output can carry file *contents* (`log` cannot: its patch flags are not
# allowed), so layers 2 and 3 apply here. `read_file` would refuse the same bytes, and
# history, strictly more than the worktree, must not be the way round.
GIT_CONTENT_COMMANDS = frozenset({"show", "diff", "blame"})

# Flags whose value may arrive as the next token, which is then a value rather than a
# revision. Of the allowlisted flags only these two accept it separated; `-U`, `--unified`,
# `--format` and `--pretty` take theirs attached or refuse.
GIT_VALUE_FLAGS = frozenset({"-L", "--date"})

_PATCH_HEADER = re.compile(r"^diff --(?:git a/(?P<a>.+) b/(?P<b>.+)|(?:cc|combined) (?P<c>.+))$")


def _revisions(checked: list[str]) -> list[str]:
    """The tokens git will read as revisions: not a flag, and not a flag's value."""
    out: list[str] = []
    after_value_flag = False
    for token in checked:
        if after_value_flag:
            after_value_flag = False
            continue
        if token.startswith("-"):
            after_value_flag = token in GIT_VALUE_FLAGS
            continue
        out.append(token)
    return out


def _require_commits(scope: str, command: str, revisions: list[str]) -> None:
    """Every revision names a commit, never a blob, a tree or `<rev>:<path>`.

    `<rev>:<path>` and a bare blob id are how a file's bytes come back without the path
    ever being named in `paths`, so no path check could see them. A range is checked side
    by side.
    """
    for token in revisions:
        if ":" in token:
            raise ToolRefused(
                f"{token!r} names a file inside a revision. 'git {command}' here takes "
                f"commits only: put the revision in 'args' and the file in 'paths', where "
                f"the path policy can check it."
            )
        sides = [s for s in re.split(r"\.{2,3}", token) if s]
        for side in sides:
            code, _, _ = _run_git(["git", "--no-pager", "-C", scope, "rev-parse", "--verify",
                                   "--quiet", "--end-of-options", f"{side}^{{commit}}"])
            if code != 0:
                raise ToolRefused(
                    f"{side!r} is not a commit in this repository. 'git {command}' here "
                    f"takes commits only -- a blob or a tree id would hand back a file's "
                    f"contents without the path policy seeing its name."
                )


def _path_refusal(cfg: Config, top: str, rel: str) -> str | None:
    """Why layers 2 or 3 refuse this repository-relative path, or None.

    The same two predicates `read_file` applies, called rather than copied, so a history
    path and a worktree path are judged by one rule. Layer 4 is not asked: a tracked file
    is not ignored in any sense git reports, and history holds no ignore state to consult.
    `rel` is joined onto the repository top level, so layer 2's per-repository additions
    apply to history paths the way they apply to the worktree (ADR-0116).
    """
    rel = rel.replace("\\", "/")
    refusal = extension_refusal(cfg, posixpath.join(top, rel))
    if refusal is not None:
        return refusal.reason
    glob = secret_match(rel, load_secret_globs(cfg))
    if glob is not None:
        return f"it matches the secret denylist pattern {glob!r}."
    return None


def _withhold_refused_sections(cfg: Config, top: str, out: str) -> str:
    """Drop the patch body of every file the path policy refuses, and say so.

    The header survives, so the reply still says the file changed -- names are not what
    the policy withholds, contents are. A quoted path is git escaping something unusual,
    and it is withheld rather than unescaped here by a second parser.
    """
    kept: list[str] = []
    withholding = False
    for line in out.splitlines():
        header = _PATCH_HEADER.match(line)
        if header:
            path = header.group("b") or header.group("c") or ""
            reason = ("its name is quoted, so it is not checked here"
                      if path.startswith('"') else _path_refusal(cfg, top, path))
            withholding = reason is not None
            kept.append(line)
            if withholding:
                kept.append(f"[contents withheld: the path policy refuses this file -- {reason}]")
            continue
        if not withholding:
            kept.append(line)
    return "\n".join(kept)


def _git_toplevel(cfg: Config, given: str) -> str:
    """The repository root for `given`, proven to still be inside a workspace root.

    Two resolutions; the second closes a real hole. `-C <dir>` makes git *discover* a
    repository by walking up, so a validated directory can resolve to a repository above the
    workspace root, whose history exposes strictly more. So the discovered top level faces
    the same check the caller's path did.
    """
    scope = resolve_search_root(cfg, given)
    if not os.path.isdir(scope):
        raise ToolRefused(f"{given!r} is not a directory, so it is not a repository.")
    code, out, err = _run_git(["git", "--no-pager", "-C", scope, "rev-parse",
                               "--show-toplevel"])
    if code != 0:
        raise ToolRefused(
            f"{given!r} is not inside a git repository ({err.strip() or 'no toplevel'})."
        )
    top = out.strip()
    if not top:
        raise ToolRefused(f"{given!r} is not inside a git repository.")
    # Re-validated rather than trusted: this is a path git chose, not one the caller wrote,
    # and layer 1 has never seen it.
    return resolve_search_root(cfg, top)


def _require_trusted_config(scope: str) -> None:
    """Refuse a repository whose own config could make git run a program.

    Called before anything but the `rev-parse` that found the repository, because every
    later command can run what that config names. The evidence is in
    docs/specs/2026-09-23-host-acted-paths.md.
    """
    key = untrusted_git_config(scope)
    if key is not None:
        raise ToolRefused(
            f"git will not run in {scope}: its own config sets {key!r}, which is not on "
            f"the list of keys host-side git trusts. Repository config can name a program "
            f"for git to run, and a repository a delegation created holds config the model "
            f"wrote. read_file still reads its files."
        )


def _read_git(cfg: Config, args: dict[str, object]) -> str:
    """Read git history, in the server process and outside the sandbox.

    `.git/**` is on the secret denylist, so inside the sandbox every git command exits 128.
    That stays: `.git` holds everything ever committed, including what left the worktree, so
    a bind would expose strictly more. This is the other way in: a fixed allowlist, no
    shell, and a repository proven inside a workspace root. Why: ADR-0010.

    One guarantee `read_file` has that this cannot: `git -C` takes a string, not a
    descriptor, so ADR-0049's check-then-use closure is unavailable by construction. The
    repository is validated twice instead, as written and as git resolved it.
    """
    command = _text_arg(args, "command")
    if command not in GIT_SUBCOMMANDS:
        raise ToolRefused(
            f"{command!r} is not a git subcommand this tool runs. Accepted: "
            f"{', '.join(sorted(GIT_SUBCOMMANDS))}. Anything that writes, fetches or "
            f"pushes is refused by design, not missing by oversight."
        )
    # Arguments first: those checks are pure, cheap and the security-relevant ones, while
    # resolving the repository runs git; and a bad flag should not report as a path problem.
    raw_args, raw_paths = _split_separator(args.get("args"), args.get("paths"))
    checked = _checked_args(command, raw_args)
    paths = _checked_paths(raw_paths)
    scope = _git_toplevel(cfg, _text_arg(args, "repo"))
    _require_trusted_config(scope)
    if command in GIT_CONTENT_COMMANDS:
        _require_commits(scope, command, _revisions(checked))
        for rel in paths:
            reason = _path_refusal(cfg, scope, rel)
            if reason is not None:
                raise ToolRefused(
                    f"{rel!r} is refused by the path policy: {reason} 'git {command}' "
                    f"would return its contents, which read_file refuses too."
                )

    fixed = [
        *(["--no-textconv"] if command in GIT_NO_TEXTCONV else []),
        *(["--no-ext-diff"] if command in GIT_NO_EXT_DIFF else []),
    ]
    argv = ["git", "--no-pager", "-C", scope, command, *fixed, *checked]
    if command == "shortlog" and not any(not a.startswith("-") for a in checked):
        # `shortlog` reads stdin, not HEAD, by default; closed, it prints nothing, which
        # reads as "no commits by anyone". Supplying the revision fixes that.
        argv.append("HEAD")
    if paths:
        argv += ["--", *paths]
    code, out, err = _run_git(argv)

    if code != 0:
        raise ToolRefused(
            f"git {command} exited {code}: {err.strip() or out.strip() or 'no output'}"
        )
    if not out.strip():
        return (
            f"git {command} produced no output and succeeded, so the answer is empty "
            f"rather than missing -- no commits, no changes, or nothing matched."
        )
    if command in GIT_CONTENT_COMMANDS:
        out = _withhold_refused_sections(cfg, scope, out)
        # The last net, as `read_file` has one: a file whose name and extension are both
        # allowed can still hold a key, and history keeps one after the worktree drops it.
        marker = key_material_marker(out.encode("utf-8", "replace"))
        if marker is not None:
            raise ToolRefused(
                f"The output of 'git {command}' contains key material -- it includes "
                f"{marker!r} -- so none of it is returned. Narrow it with 'paths' to the "
                f"files you need."
            )

    # Bounded by `max_read_chars`, not a third setting; cut on a line boundary and said,
    # since a cut history read as complete makes a model deny a commit exists.
    lines = out.splitlines()
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if kept and used + len(line) + 1 > cfg.max_read_chars:
            return (
                "\n".join(kept)
                + f"\n\n[truncated: {index} of {len(lines)} lines. Narrow it with a "
                f"revision range, a path, or --max-count.]"
            )
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


READ_FILE = RegisteredTool(
    spec=ToolSpec(
        name="read_file",
        description=(
            "Read a UTF-8 text file from the workspace, with every line numbered, so you "
            "can cite what you read as a file name and a line number. Paths must be "
            "absolute. Long files come back one range at a time: the result states the "
            "total number of lines and the line to continue from, so read the next range "
            "rather than the file again, and use start_line and end_line to go straight "
            "to the part you want rather than reading past it. Refused for a path outside "
            "the workspace, an unlisted extension, a file git ignores, or anything that "
            "is not text."
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
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read, counting from 1, and included in "
                                   "what comes back. Omit to read to the end of the "
                                   "file. Give it whenever you know roughly where the "
                                   "part you want ends: a tool result is resent on every "
                                   "later turn, so a range you did not need is paid for "
                                   "again each turn rather than once. Past the end of "
                                   "the file is not an error; below start_line is.",
                },
            },
            "required": ["path"],
        },
    ),
    handler=_read_file,
    cacheable=True,
    tracks_reads=True,
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
    tracks_reads=True,
)

RUN_BASH = RegisteredTool(
    spec=ToolSpec(
        name="run_bash",
        description=(
            "Run a shell command, confined: no network, an empty filesystem apart from a "
            "scratch HOME and a read-only toolchain, and your real home directory absent "
            "rather than merely unreadable. .git is hidden too, so git fails here: use "
            "read_git for history. Commands time out and are killed. The server "
            "reports the real exit code it observed, so do not describe a command as having "
            "succeeded when the result says otherwise. To change a file's text, prefer "
            "write_file, which replaces it whole. When a project has been provisioned, "
            "$DELEGATE_PYTHON is the absolute path to an interpreter with its dependencies "
            "installed -- run tests with \"$DELEGATE_PYTHON\" -m pytest, and the project's "
            "other tools the same way, e.g. \"$DELEGATE_PYTHON\" -m ruff: they are not on "
            "PATH. It is unset when "
            "nothing current is provisioned for this workdir, including when the project's "
            "dependencies have changed since; there is no way to install one from here. "
            "Tests the project declares as unable to run nested are already deselected via "
            "PYTEST_ADDOPTS, so a report of them is a deselection rather than a failure. "
            "The exit code is recorded for you and reported to whoever asked, so you do not "
            "need to echo it: the server records the status of the whole command line, and a "
            "trailing `; echo $?` or `| tail` replaces the status of the work with the "
            "status of the echo. Put the command whose result matters last. A failure "
            "earlier in the line is still detected and said back to you beside the exit "
            "code, so a zero there is not a reason to assume every step worked."
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
            "path is required and names the directory to search: it is the only argument "
            "that narrows the walk, and the workspace layout below tells you what to put "
            "there. glob narrows which of the walked files are opened, not how many are "
            "walked, so it is not a substitute. Files the path "
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
                    "description": "Absolute path to a directory or file to search, and "
                                   "the only argument that narrows the walk. Required. A "
                                   "workspace root itself is accepted but walks all of "
                                   "it, so name a directory inside one where you can. "
                                   f"Pass the exact string {UNSCOPED!r} to walk every "
                                   "workspace root -- the slowest call this server offers, "
                                   "by roughly two orders of magnitude.",
                },
                "glob": {
                    "type": "string",
                    "description": "Only open files whose NAME matches this glob, e.g. "
                                   "*.py or test_*.py. Matches the name, not the path. It "
                                   "narrows what is opened, never what is walked, so it "
                                   "does not replace path.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Most matching lines to return. Defaults to 100.",
                },
            },
            "required": ["pattern", "path"],
        },
    ),
    handler=_search_files,
    cacheable=True,
)

# Insertion order is the declared order, and it is fixed here rather than sorted at use.
EDIT_FILE = RegisteredTool(
    spec=ToolSpec(
        name="edit_file",
        description=(
            "Change part of a UTF-8 text file in the workspace by replacing exact text, "
            "leaving the rest of the file untouched. Prefer this over write_file for an "
            "edit to an existing file: write_file replaces the whole file, so it needs you "
            "to reproduce every line you are not changing. old_string must appear exactly "
            "once -- if it appears never or more than once the file is left completely "
            "unchanged and you are told which, so quote enough of the surrounding lines to "
            "be unique, and read the file first rather than quoting from memory. Set "
            "`expected_count` to replace every occurrence when the text repeats on "
            "purpose. An empty new_string deletes the text. An edit to a file that "
            "changed since you last read it is refused until you read it again. The "
            "same path rules as read_file apply."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace, including whitespace and "
                                   "indentation. Must occur exactly once in the file, "
                                   "unless expected_count says otherwise.",
                },
                "new_string": {
                    "type": "string",
                    "description": "What to put in its place. Empty to delete the text.",
                },
                "expected_count": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How many times old_string is believed to appear. "
                                   "Given, the text must appear exactly that many times "
                                   "and then all of them are replaced; a mismatch "
                                   "refuses and writes nothing. Omitted, the text must "
                                   "appear exactly once.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    ),
    handler=_edit_file,
    writes=True,
    tracks_reads=True,
)


READ_GIT = RegisteredTool(
    spec=ToolSpec(
        name="read_git",
        description=(
            "Read a repository's git history: log, show, diff, blame, ls-files, shortlog, "
            "rev-list, status and rev-parse -- and rev-list --count is how you count "
            "commits. This is the only way to reach history, because run_bash "
            "cannot see .git at all -- a git command there fails rather than telling you "
            "why. Use it for questions the worktree cannot answer: when a line changed and "
            "why, whether a claim in a document predates the code it describes, what a "
            "commit touched, which files are untracked. 'repo' is an absolute path inside "
            "the repository and the repository must sit in the workspace. Put file paths in "
            "'paths'; a '--' in 'args' works too, and what follows it is taken as paths. "
            "Only subcommands and "
            "flags on a fixed allowlist run: nothing that writes, fetches or pushes, and no "
            "flag that could name a program or a file to write, so a refusal here is the "
            "design rather than a gap. show, diff and blame take commits only -- never "
            "'<rev>:<path>' or a blob id -- and read_file's rules apply to the files they "
            "would print: a refused file is named, its contents withheld. Long output is "
            "truncated on a line boundary and says so -- read that before concluding "
            "something is absent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Absolute path to the repository, or any directory "
                                   "inside it.",
                },
                "command": {
                    "type": "string",
                    "description": "The git subcommand: log, show, diff, blame, ls-files, "
                                   "shortlog, rev-list, status or rev-parse.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Flags and revisions, e.g. [\"--oneline\", \"-n\", "
                                   "\"20\"] or [\"HEAD~5..HEAD\"]. File paths belong in "
                                   "'paths'; anything after a '--' here is taken as paths "
                                   "too. For show, diff and blame a revision must be a "
                                   "commit.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths to limit the command to, relative to the "
                                   "repository root. A path deleted long ago is fine.",
                },
            },
            "required": ["repo", "command"],
        },
    ),
    handler=_read_git,
    # Not cacheable: `run_bash` can commit mid-delegation, and a model would not think to
    # re-check a stale answer about history.
    cacheable=False,
)


REGISTRY: dict[str, RegisteredTool] = {
    t.spec.name: t
    for t in (READ_FILE, SEARCH_FILES, READ_GIT, WRITE_FILE, EDIT_FILE, RUN_BASH)
}
ALL_TOOL_NAMES: frozenset[str] = frozenset(REGISTRY)

# Derived, never written out: `delegate_readonly`'s set, and the whole of ADR-0042's
# promise that nothing it can do will write. A tool added without `writes` lands here,
# which is why tests/test_tools.py checks this against the registry, not a hand list.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    name for name, tool in REGISTRY.items() if not tool.writes
)

# Empty. `run_bash` without bubblewrap is a fact about the host, asked in
# `available_tool_names`, since a constant cannot vary per machine; this set is for a tool
# withheld *everywhere*.
#
# Kept deliberately: withholding says "this tool exists and cannot work today", a fact
# about the server, unlike a caller's `allowed_tools`, and deleting it means rebuilding it
# under pressure when something is next implemented before it is safe. It narrows only what
# is *declared*; `execute_tool` checks its own set regardless, since a model can call a tool
# it was never offered, and never consulted this one, so emptying it weakens nothing.
WITHHELD_TOOL_NAMES: frozenset[str] = frozenset()


def available_tool_names(cfg: Config) -> frozenset[str]:
    """What this server can offer today, as opposed to what it implements.

    Asks, since whether bubblewrap is here is a fact about this host, not import time.
    Declaring a tool the host cannot run costs a turn learning what the server knew; this
    is `sandbox.run`'s own refusal condition, checked before a turn is paid for.
    """
    names = ALL_TOOL_NAMES - WITHHELD_TOOL_NAMES
    if not sandbox.available(cfg):
        names -= {"run_bash"}
    if not git_available():
        # As `run_bash` above: an unrunnable tool costs a turn to discover.
        names -= {"read_git"}
    return names


def resolve_allowed(requested: Iterable[str] | None, cfg: Config) -> frozenset[str]:
    """The resolved set for one delegation: what was asked for, minus what cannot work.

    `None` means "whatever is available". An explicit set is narrowed, never widened:
    intersecting, not unioning, stops a caller getting a tool this server withholds, one the
    host cannot run, or one it does not implement.
    """
    available = available_tool_names(cfg)
    if requested is None:
        return available
    return frozenset(requested) & available


# --- the two enforcement sites ------------------------------------------------------------


def _with_layout(spec: ToolSpec, cfg: Config) -> ToolSpec:
    """`search_files` with this deployment's workspace map appended to its description.

    Not stored in `REGISTRY`, which `gen_tools_docs.py` renders into the committed
    `docs/TOOLS.md`: a workspace root is an absolute path on the operator's machine, home
    included, so a map there would publish a local layout, and a personal identifier in it,
    into a public artefact. The generator has no `Config`, so renders only the contract.
    """
    if spec.name != SEARCH_FILES.spec.name:
        return spec
    return replace(spec, description=f"{spec.description}\n\n{_scope_help(cfg)}")


def declared_tools(cfg: Config, allowed: Iterable[str]) -> tuple[ToolSpec, ...]:
    """Site one: what the model is offered.

    Registry order, so a resolved set always renders the same bytes in the cached prefix.
    Editing a description below is a prefill bill as well as a contract change: it moves
    the prefix's first difference toward the front (measured, JOURNAL 2026-09-02).
    """
    permitted = set(allowed)
    return tuple(
        _with_layout(t.spec, cfg) for name, t in REGISTRY.items() if name in permitted
    )


# A shell command that rewrites file text, which `write_file` does better; the note lands
# in that call's own result, where the next decision is made. Why: ADR-0024. In-place
# editors are named explicitly, since a miss costs nothing and a false positive only a
# note; redirection is the one general case, minus device targets and fd duplications.
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

    Advisory, erring quiet, and never a reason to block.
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
    seen: SeenFiles | None = None,
) -> ToolResultBlock:
    """Site two: what actually runs.

    Checked against `allowed` independently of what was declared: the declared list is a
    suggestion the model can ignore, and naming a tool it was never offered is exactly the
    case worth catching.

    A refusal is an error result, not an exception: aborting is right for prefetch, where
    nothing is spent yet, but mid-loop it would discard every paid turn over one bad call.
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
        if tool.wants_policy:
            produced = tool.handler(cfg, call.input, policy or BashPolicy())
        elif tool.tracks_reads:
            produced = tool.handler(cfg, call.input, seen)
        else:
            produced = tool.handler(cfg, call.input)
    except (ToolRefused, PathRefused, PathPolicyError) as e:
        return ToolResultBlock(tool_use_id=call.id, content=str(e), is_error=True)
    if isinstance(produced, BashResult):
        text = produced.text
        # Gated on `permitted`, the set enforced here, never what was declared: steering to
        # a tool the executor would refuse is worse than quiet (ADR-0024).
        if "write_file" in permitted and _rewrites_text(str(call.input.get("command") or "")):
            text = text + "\n\n" + STEER
        return ToolResultBlock(
            tool_use_id=call.id,
            content=text,
            is_error=produced.is_error,
            bash=produced.outcome,
        )
    return ToolResultBlock(tool_use_id=call.id, content=produced)
