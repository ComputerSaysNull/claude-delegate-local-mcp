"""Prefetch: read the files the caller named, and assemble them into the prompt.

This is the point of the whole project. Claude names a file; the server reads it and hands
the bytes to a model running on the user's own hardware. The bytes never enter Claude's
context, which is what makes delegating a review of a large file cheaper than reading it.

`paths.py` has already decided *whether* a file may be read. This module decides whether
it is worth reading and what it costs, which is a different question with a different
answer shape: a refusal fails the call, while everything here is a **skip** -- the call
proceeds, the file is left out, and the accounting says which and why. A caller that asked
for six files and got five must be told, in the result and in the prompt, or five reads
like six.

Three things are load-bearing:

**Never read a file only to discover it was unusable.** Size is checked by `stat`, and the
token estimate is computed from the stat size, so an over-budget file costs one syscall
rather than a multi-megabyte read.

**Skip whole, never truncate.** A source file cut mid-function is worse than an absent
one, because the model will confidently repair code it never saw.

**Order is fixed and caller-independent.** The cluster caches prompt prefixes, so the file
list is sorted by resolved path before anything is accumulated -- including before the
total-budget cutoff, or the same six files in a different order would produce a different
five. (ADR-0011)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Config
from .paths import ResolvedPath

# How far in to look for a NUL byte. A text file does not contain one, and a binary file
# that hides its first NUL past 8 KiB is rarer than the cost of reading further. ADR-0030.
NUL_SNIFF_BYTES = 8192

SKIP_TOO_MANY_BYTES = "too_many_bytes"
SKIP_OVER_FILE_BUDGET = "over_file_budget"
SKIP_BINARY = "binary"
SKIP_OVER_TOTAL_BUDGET = "over_total_budget"
SKIP_UNREADABLE = "unreadable"

# The markers wrapping each file. Not a markdown fence: an inlined `.md` file carries
# fences of its own, and the model would read the first one as the end of the file.
BEGIN = "--- BEGIN FILE {path} ---"
END = "--- END FILE {path} ---"

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

        Deliberately built from the *resolved* path rather than the one the caller wrote.
        Two spellings of one file -- a symlink and its target, a forward-slash and a
        backslash form -- would otherwise render as two different prompts for identical
        content, which is exactly the prefix-cache miss the ordering rule exists to avoid.
        """
        parts: list[str] = []
        if self.files:
            parts.append(FILES_HEADER)
            for entry in self.files:
                body = entry.text if entry.text.endswith("\n") else entry.text + "\n"
                parts.append(
                    BEGIN.format(path=entry.path) + "\n" + body + END.format(path=entry.path)
                )
        if self.skips:
            parts.append(SKIPS_HEADER)
            parts.extend(f"{s.path} -- {s.reason}" for s in self.skips)
        return "\n\n".join(parts)

    def accounting(self) -> dict[str, object]:
        """The same facts as data, returned beside the answer.

        The prompt tells the model what it did not get; this tells the caller. They are
        different audiences: the model needs to not hallucinate the file, and the caller
        needs to decide whether to re-ask with fewer of them.
        """
        return {
            # Both spellings: `path` is what the server read and what the model was
            # shown, `given` is what the caller wrote. Reporting only the resolved one
            # makes the caller match `/mnt/c/...` against the Windows path they sent.
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


def decode_text(data: bytes) -> tuple[str | None, str]:
    """Decode as text, or say why it is binary. ADR-0030.

    Two tests, because neither alone is enough. The NUL byte catches UTF-16 and most
    executables cheaply. The strict UTF-8 decode catches the rest -- a latin-1 file, a
    truncated multi-byte sequence -- and it has to be strict: decoding with `errors=
    "replace"` would hand the model a page of U+FFFD and call it source.

    Extension is not a third test, because it cannot be one. The allowlist admits `.json`
    and `.md`, and nothing stops either from being UTF-16.
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


def _stat_size(path: str) -> tuple[int, str]:
    try:
        return os.stat(path).st_size, ""
    except OSError as e:
        # It passed the path policy moments ago, so this is a race or a permission
        # problem rather than a caller error -- a skip, not a refusal.
        return -1, f"it could not be read ({e.strerror or e})"


def prefetch(cfg: Config, resolved: tuple[ResolvedPath, ...]) -> Prefetch:
    """Read what fits, skip what does not, and account for both.

    Per file, in this order, because the point is never to read a file only to find out
    it was unusable:

    1. `stat` against `max_file_read_bytes` -- a multi-gigabyte file is never loaded.
    2. Estimate tokens from the stat size and the extension (ADR-0019), before reading.
    3. Over `max_file_tokens`: skip whole. Never truncate.
    4. Against `max_total_prefetch_tokens`: the first file that does not fit ends the
       list, and every file after it is skipped too.
    5. Only now read it, and decide whether it is text at all.

    Step 4 stopping rather than continuing is a decision, not an oversight. Carrying on to
    fit whatever happens to be small enough makes the result depend on the size mix in a
    way nobody can predict from the request, and it is worse for the caller: a coherent
    prefix of the files they asked for beats an arbitrary subset of them.
    """
    entries: list[FileEntry] = []
    skips: list[Skip] = []
    total = 0
    exhausted = False

    # Sorted here, once, before anything is accumulated. Doing it later -- or leaving it
    # to the caller -- would make the total-budget cutoff depend on the order the files
    # were named in, so the same request could return a different five of six.
    for item in sorted(resolved, key=lambda r: r.posix):
        if exhausted:
            skips.append(
                Skip(
                    item.posix,
                    item.given,
                    SKIP_OVER_TOTAL_BUDGET,
                    f"the {cfg.max_total_prefetch_tokens}-token budget for this call was "
                    "already spent by an earlier file in the list",
                )
            )
            continue

        nbytes, error = _stat_size(item.posix)
        if nbytes < 0:
            skips.append(Skip(item.posix, item.given, SKIP_UNREADABLE, error))
            continue

        if nbytes > cfg.max_file_read_bytes:
            skips.append(
                Skip(
                    item.posix,
                    item.given,
                    SKIP_TOO_MANY_BYTES,
                    f"it is {nbytes} bytes, over the {cfg.max_file_read_bytes}-byte hard "
                    "ceiling, so it was not read at all",
                )
            )
            continue

        est = cfg.estimate_tokens(nbytes, item.ext)
        if est > cfg.max_file_tokens:
            skips.append(
                Skip(
                    item.posix,
                    item.given,
                    SKIP_OVER_FILE_BUDGET,
                    f"an estimated {est} tokens exceeds the {cfg.max_file_tokens} "
                    "per-file limit. It is left out whole rather than truncated, because "
                    "source cut mid-function is worse than absent",
                )
            )
            continue

        if total + est > cfg.max_total_prefetch_tokens:
            exhausted = True
            skips.append(
                Skip(
                    item.posix,
                    item.given,
                    SKIP_OVER_TOTAL_BUDGET,
                    f"the {cfg.max_total_prefetch_tokens}-token budget for this call was "
                    f"at {total}, and this file needs about {est} more. Everything after "
                    "it in the list was skipped too",
                )
            )
            continue

        try:
            with open(item.posix, "rb") as fh:
                data = fh.read()
        except OSError as e:
            skips.append(
                Skip(
                    item.posix,
                    item.given,
                    SKIP_UNREADABLE,
                    f"it could not be read ({e.strerror or e})",
                )
            )
            continue

        text, why = decode_text(data)
        if text is None:
            skips.append(Skip(item.posix, item.given, SKIP_BINARY, why))
            continue

        total += est
        entries.append(
            FileEntry(
                path=item.posix,
                given=item.given,
                text=text,
                nbytes=nbytes,
                est_tokens=est,
            )
        )

    return Prefetch(
        files=tuple(entries),
        skips=tuple(skips),
        total_tokens=total,
        budget=cfg.max_total_prefetch_tokens,
    )
