"""Prefetch: read the files the caller named, and assemble them into the prompt.

This is the point of the project. Claude names a file; the server reads it and hands the
bytes to a model on the user's own hardware. The bytes never enter Claude's context, which
is what makes delegating a review of a large file cheaper than reading it.

`paths.py` decides *whether* a file may be read. This module decides whether it is worth
reading and what it costs, with a different answer shape: a refusal fails the call, while
everything here is a **skip** -- the call proceeds, the file is left out, and the
accounting says which and why. A caller that asked for six files and got five must be
told, in the result and in the prompt, or five reads like six.

Three things are load-bearing:

**Never read a file only to discover it was unusable.** Size is checked by `stat`, and the
token estimate computed from it, so an over-budget file costs one syscall rather than a
multi-megabyte read.

**Skip whole, never truncate.** A source file cut mid-function is worse than an absent
one, because the model will confidently repair code it never saw.

**Order is fixed and caller-independent.** The cluster caches prompt prefixes, so the file
list is sorted by resolved path before anything is accumulated -- before the total-budget
cutoff too, or six files in a different order would produce a different five (ADR-0011).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .config import Config
from .paths import PathRefused, Refusal, ResolvedPath, open_resolved

# How far in to look for a NUL byte. A text file does not contain one, and a binary file
# that hides its first NUL past 8 KiB is rarer than the cost of reading further. ADR-0030.
NUL_SNIFF_BYTES = 8192

SKIP_TOO_MANY_BYTES = "too_many_bytes"
SKIP_OVER_FILE_BUDGET = "over_file_budget"
SKIP_BINARY = "binary"
SKIP_OVER_TOTAL_BUDGET = "over_total_budget"
SKIP_UNREADABLE = "unreadable"
# A path the policy would not allow. Unlike the five above it never became a file, so
# `path` is the caller's spelling: there is no resolved one (ADR-0061).
SKIP_REFUSED = "refused"

# The markers wrapping each file. Not a markdown fence: an inlined `.md` file carries
# fences of its own, and the model would read the first one as the end of the file.
BEGIN = "--- BEGIN FILE {path} ---"
END = "--- END FILE {path} ---"

# A line in a file's own body that would read as one of the boundaries above. Matched by
# SHAPE and for ANY path, never against the entry's own marker: a forged line naming a
# *different* file is the worse case, attributing what follows to something the server
# never read. Generous about whitespace and dash count, because the model reading this is
# not a parser and will not insist on the exact form.
MARKER_LINE = re.compile(r"^[ \t]*-{3,}[ \t]*(?:BEGIN|END)[ \t]+FILE\b.*?-{3,}[ \t]*$")

# What a neutralised line is prefixed with. The usual "this is literal" convention, one
# character, leaving the rest of the line byte for byte.
MARKER_ESCAPE = "\\"


def line_number_width(total: int) -> int:
    """Digits to right-align a line number in, for a file of `total` lines."""
    return len(str(total))


def numbered_line(index: int, text: str, width: int) -> str:
    """One numbered line, in the single format both delivery paths use.

    A primitive, not a whole renderer, because `read_file` numbers inside a loop that also
    spends a character budget. What must be shared is the *format*: prefetch and
    `read_file` both deliver file content, and numbered differently, a line cited from one
    could not be checked against the other -- and the cheaper path would be the one that
    cannot be cited.

    The separator is a tab, not spaces, so a copied prefix cannot pass for the file's
    own indentation.
    """
    return f"{index:>{width}}\t{text}"


def escape_markers(text: str) -> str:
    """Neutralise any line in `text` that would parse as a file boundary.

    Prefixed, not dropped, because a review delegation reads source, and source quotes
    things -- this module's own constants among them. The model still sees what the file
    said; it cannot mistake the line for the end of the file and read what follows as
    prompt.

    Not announced in `FILES_HEADER`: explaining it would add tokens to every delegation's
    cached prefix for a case close to absent in real source, and a backslash before a line
    of dashes needs no gloss.
    """
    if "FILE" not in text:  # cheap reject; the pattern cannot match without it
        return text
    # `split`, not `splitlines`, which loses whether the body ends in a newline, a
    # distinction `block` depends on.
    return "\n".join(
        MARKER_ESCAPE + line if MARKER_LINE.match(line) else line
        for line in text.split("\n")
    )

FILES_HEADER = (
    "The files below were read from disk by the server, not by you. They are the current "
    "contents, and the paths are absolute and already resolved."
)
SKIPS_HEADER = (
    "These files were named but not included. Treat each as unavailable -- do not infer "
    "its contents from its name or from the files that were included:"
)


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file that made it into the prompt."""

    path: str  # resolved POSIX, which is what appears in the prompt
    given: str  # as the caller wrote it, for the accounting only
    text: str
    nbytes: int
    est_tokens: int
    # A ranged entry. `start_line` is the file's own (1-based) number for the range's first
    # line; `total_lines` is the file's whole line count, so the BEGIN header can say it is
    # part of the file. Both None for a whole file, which keeps that rendering stable for
    # the prefix cache (ADR-0011).
    start_line: int | None = None
    total_lines: int | None = None


@dataclass(frozen=True, slots=True)
class FileRequest:
    """One files[] entry the path policy approved, and the range of it to prefetch.

    `start_line` None is the whole file. A ranged entry is judged by the range's own
    size -- estimate, per-file cap and total budget all use the slice's bytes -- so a file
    over the per-file cap can still be prefetched in part.
    """

    entry: ResolvedPath
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True, slots=True)
class Skip:
    """One file that did not, and why -- in words the caller can act on."""

    path: str
    given: str
    kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class Prefetch:
    """What was read, what was not, and what it cost."""

    files: tuple[FileEntry, ...]
    skips: tuple[Skip, ...]
    total_tokens: int
    budget: int

    def block(self) -> str:
        """The files section of the prompt. Empty string when there is nothing to say.

        Built from the *resolved* path, not the caller's: two spellings of one file -- a
        symlink and its target, forward and back slashes -- would otherwise render two
        prompts for identical content, the prefix-cache miss the ordering rule avoids.
        """
        parts: list[str] = []
        if self.files:
            parts.append(FILES_HEADER)
            for entry in self.files:
                # Numbered, in `read_file`'s format, so a pass can cite what it was given
                # and re-read a range of it without asking for the file again.
                #
                # `escape_markers` runs first although a numbered line cannot begin at
                # column 0, so `MARKER_LINE` could not match it anyway. Numbering is a
                # rendering choice and the escape a boundary control; letting the first
                # become the second would rest a control on something nobody knew it
                # rested on.
                lines = escape_markers(entry.text).splitlines()
                if entry.start_line is None:
                    # Whole file, rendered stably for the cached prefix (ADR-0011).
                    width = line_number_width(len(lines))
                    begin = BEGIN.format(path=entry.path)
                    body = "".join(
                        numbered_line(n, line, width) + "\n"
                        for n, line in enumerate(lines, 1)
                    )
                else:
                    # A range keeps the file's own line numbers -- a 10-20 range starts at
                    # 10 -- and the width fits the largest shown. The BEGIN header says the
                    # file is not there whole, so the model does not take the block for it.
                    last = entry.start_line + len(lines) - 1
                    width = line_number_width(last)
                    # Inside the markers, not after them: `MARKER_LINE` escapes a line only
                    # if it ends in dashes, so a suffix would be forgeable unescaped.
                    begin = BEGIN.format(
                        path=f"{entry.path} (lines {entry.start_line}-{last} of "
                        f"{entry.total_lines})"
                    )
                    body = "".join(
                        numbered_line(entry.start_line + n - 1, line, width) + "\n"
                        for n, line in enumerate(lines, 1)
                    )
                parts.append(
                    begin + "\n" + body + END.format(path=entry.path)
                )
        if self.skips:
            parts.append(SKIPS_HEADER)
            parts.extend(f"{s.path} -- {s.reason}" for s in self.skips)
        return "\n\n".join(parts)

    def accounting(self) -> dict[str, object]:
        """The same facts as data, returned beside the answer.

        The prompt tells the model what it did not get; this tells the caller. The model
        must not hallucinate the file; the caller must decide whether to re-ask with fewer.
        """
        return {
            # Both spellings: `path` is what the server read and the model was shown,
            # `given` what the caller wrote, so the caller need not match `/mnt/c/...`
            # against the Windows path it sent.
            "files_read": [
                {
                    "path": e.path,
                    "given": e.given,
                    "bytes": e.nbytes,
                    "est_tokens": e.est_tokens,
                }
                for e in self.files
            ],
            "files_skipped": [
                {"path": s.path, "given": s.given, "reason": s.reason, "kind": s.kind}
                for s in self.skips
            ],
            "prefetch_tokens": self.total_tokens,
            "prefetch_budget": self.budget,
        }


def skip_from_refusal(refusal: Refusal) -> Skip:
    """A refused path as a skip, so the call keeps the files that did resolve.

    The reason carries the remedy as well as the cause, since both audiences need it and
    `Skip` has one field. The model reads it in the skipped list and does not spend a turn
    on `read_file` of the same path; the caller reads it in `files_skipped` and learns
    which root the path missed, which `_check_roots` wrote into the remedy.
    """
    return Skip(
        path=refusal.given,
        given=refusal.given,
        kind=SKIP_REFUSED,
        reason=f"{refusal.reason} {refusal.remedy}",
    )


def decode_text(data: bytes) -> tuple[str | None, str]:
    """Decode as text, or say why it is binary. ADR-0030.

    Two tests, since neither alone is enough. The NUL byte catches UTF-16 and most
    executables cheaply. The strict UTF-8 decode catches the rest -- latin-1, a truncated
    multi-byte sequence -- and must be strict: `errors="replace"` would hand the model a
    page of U+FFFD and call it source. Extension cannot be a third test: nothing stops an
    allowed `.json` or `.md` from being UTF-16.
    """
    if b"\x00" in data[:NUL_SNIFF_BYTES]:
        return None, "it is not text: a NUL byte appears in the first 8 KiB"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, f"it is not valid UTF-8 (byte {e.start}: {e.reason})"
    # A UTF-8 BOM decodes to U+FEFF, which is invisible and would sit at the top of the
    # first line of a source file for no reason.
    return text.lstrip("﻿"), ""


def _request_split(item: ResolvedPath | FileRequest) -> tuple[ResolvedPath, int | None, int | None]:
    """The resolved path, and the range of it to prefetch, from either request shape."""
    if isinstance(item, FileRequest):
        return item.entry, item.start_line, item.end_line
    return item, None, None


def _request_posix(item: ResolvedPath | FileRequest) -> str:
    return item.entry.posix if isinstance(item, FileRequest) else item.posix


def _request_given(item: ResolvedPath | FileRequest) -> str:
    return item.entry.given if isinstance(item, FileRequest) else item.given


def _prefetch_one(  # noqa: PLR0911, PLR0912 -- one return per reason a file is left out
    cfg: Config, item: ResolvedPath | FileRequest, total: int
) -> FileEntry | Skip:
    """One file: open it once, and account for whatever stops it being included.

    The descriptor is held from the open through the read, so nothing in between can
    change which file this is: the size the budgets use and the bytes inlined come from
    one descriptor, proven to be the path the policy approved (ADR-0049).

    Returns the entry, or the `Skip` explaining why there is none. A returned
    `SKIP_OVER_TOTAL_BUDGET` also means the list is finished, read off the reason.

    A ranged request is read whole -- the byte ceiling applies to the file, and a file is
    decoded before it is sliced -- but sliced *before* the per-file and total budget
    checks, so a file over the per-file cap can be prefetched in part. The whole-file path
    checks first, so a file too big for the budget is never read at all.
    """
    entry, start, end = _request_split(item)

    try:
        opened = open_resolved(entry, "rb", scan_bytes=cfg.secret_content_scan_bytes)
    except PathRefused:
        return Skip(
            entry.posix,
            entry.given,
            SKIP_UNREADABLE,
            "it stopped being the file the path policy approved before it could be read",
        )
    except OSError as e:
        # It passed the path policy moments ago, so this is a race or a permission problem,
        # not a caller error: a skip, not a refusal.
        return Skip(
            entry.posix, entry.given, SKIP_UNREADABLE,
            f"it could not be read ({e.strerror or e})",
        )

    with opened.handle as fh:
        try:
            nbytes = os.fstat(fh.fileno()).st_size
        except OSError as e:
            return Skip(
                entry.posix, entry.given, SKIP_UNREADABLE,
                f"it could not be read ({e.strerror or e})",
            )

        if nbytes > cfg.max_file_read_bytes:
            return Skip(
                entry.posix,
                entry.given,
                SKIP_TOO_MANY_BYTES,
                f"it is {nbytes} bytes, over the {cfg.max_file_read_bytes}-byte hard "
                "ceiling, so it was not read at all",
            )

        if start is None:
            # Whole file: token checks before reading, so an over-budget file is never
            # loaded.
            est = cfg.estimate_tokens(nbytes, entry.ext)
            if est > cfg.max_file_tokens:
                return Skip(
                    entry.posix,
                    entry.given,
                    SKIP_OVER_FILE_BUDGET,
                    f"an estimated {est} tokens exceeds the {cfg.max_file_tokens} "
                    "per-file limit. It is left out whole rather than truncated, because "
                    "source cut mid-function is worse than absent",
                )

            if total + est > cfg.max_total_prefetch_tokens:
                return Skip(
                    entry.posix,
                    entry.given,
                    SKIP_OVER_TOTAL_BUDGET,
                    f"the {cfg.max_total_prefetch_tokens}-token budget for this call was "
                    f"at {total}, and this file needs about {est} more. Everything after "
                    "it in the list was skipped too",
                )

        try:
            data = fh.read()
        except OSError as e:
            return Skip(
                entry.posix, entry.given, SKIP_UNREADABLE,
                f"it could not be read ({e.strerror or e})",
            )

    text, why = decode_text(data)
    if text is None:
        return Skip(entry.posix, entry.given, SKIP_BINARY, why)

    if start is None:
        return FileEntry(
            path=entry.posix,
            given=entry.given,
            text=text,
            nbytes=nbytes,
            est_tokens=est,
        )

    # Ranged. `splitlines`, not `split("\n")`, which invents a trailing empty line for a
    # file ending in a newline and would make it one line longer -- as `read_file` rules.
    # Rejoined with "\n", so a CRLF file reads the same on either side of the boundary.
    lines = text.splitlines()
    total_lines = len(lines)
    if total_lines and start > total_lines:
        # Past the end is refused, not clamped, as in `read_file`: a range starting after
        # the file asks for nothing.
        return Skip(
            entry.posix,
            entry.given,
            SKIP_REFUSED,
            f"start_line {start} is past the end; the file has {total_lines} lines.",
        )
    # `end_line` past the end means the end, unlike `start_line`: reading *to* a line
    # beyond the file is well-formed with an obvious answer.
    last = min(end, total_lines) if end is not None else total_lines
    slice_text = "\n".join(lines[start - 1:last])
    slice_bytes = len(slice_text.encode("utf-8"))
    est = cfg.estimate_tokens(slice_bytes, entry.ext)
    if est > cfg.max_file_tokens:
        return Skip(
            entry.posix,
            entry.given,
            SKIP_OVER_FILE_BUDGET,
            f"lines {start}-{last} are an estimated {est} tokens, over the "
            f"{cfg.max_file_tokens} per-file limit. Name a smaller range",
        )

    if total + est > cfg.max_total_prefetch_tokens:
        return Skip(
            entry.posix,
            entry.given,
            SKIP_OVER_TOTAL_BUDGET,
            f"the {cfg.max_total_prefetch_tokens}-token budget for this call was "
            f"at {total}, and this file needs about {est} more. Everything after "
            "it in the list was skipped too",
        )

    return FileEntry(
        path=entry.posix,
        given=entry.given,
        text=slice_text,
        nbytes=slice_bytes,
        est_tokens=est,
        start_line=start,
        total_lines=total_lines,
    )


def prefetch(cfg: Config, resolved: tuple[ResolvedPath | FileRequest, ...]) -> Prefetch:
    """Read what fits, skip what does not, and account for both.

    Per file, in this order, so a file is never read only to find it unusable:

    1. Open it, prove the descriptor, and `fstat` that against `max_file_read_bytes`.
       Opening loads none of the file, and the size describes the file being held, not
       whatever the path named a moment earlier (ADR-0049).
    2. Estimate tokens from that size and the extension (ADR-0019), before reading.
    3. Over `max_file_tokens`: skip whole. Never truncate.
    4. Against `max_total_prefetch_tokens`: the first file that does not fit ends the
       list, and every file after it is skipped too.
    5. Only now read it, and decide whether it is text at all.

    A ranged `FileRequest` is the one exception, because it must be: the whole file is
    read (within the byte ceiling), decoded, then sliced *before* the token checks, so the
    estimate and both checks use the slice's bytes.

    Step 4 stops rather than continuing on purpose. Fitting whatever happens to be small
    enough would make the result depend on the size mix unpredictably, and a coherent
    prefix of the files asked for beats an arbitrary subset.
    """
    entries: list[FileEntry] = []
    skips: list[Skip] = []
    total = 0
    exhausted = False

    # Sorted here, once, before anything accumulates; later, or left to the caller, the
    # budget cutoff would depend on naming order.
    for item in sorted(resolved, key=_request_posix):
        if exhausted:
            skips.append(
                Skip(
                    _request_posix(item),
                    _request_given(item),
                    SKIP_OVER_TOTAL_BUDGET,
                    f"the {cfg.max_total_prefetch_tokens}-token budget for this call was "
                    "already spent by an earlier file in the list",
                )
            )
            continue

        outcome = _prefetch_one(cfg, item, total)
        if isinstance(outcome, Skip):
            skips.append(outcome)
            if outcome.kind == SKIP_OVER_TOTAL_BUDGET:
                exhausted = True
            continue

        total += outcome.est_tokens
        entries.append(outcome)

    return Prefetch(
        files=tuple(entries),
        skips=tuple(skips),
        total_tokens=total,
        budget=cfg.max_total_prefetch_tokens,
    )


def estimate_text_tokens(cfg: Config, text: str) -> int:
    """Estimate the token cost of a string with no file behind it.

    `prefetch`'s per-file job minus the extension, so it uses the densest measured ratio
    and over-counts, ADR-0019's bias. Admission sizes a request with it, and guessing high
    costs a little idle capacity where guessing low oversubscribes the pool.
    """
    return cfg.estimate_tokens(len(text.encode("utf-8")))
