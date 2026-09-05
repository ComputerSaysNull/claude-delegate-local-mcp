r"""The four-layer path policy: what a delegated model is allowed to be shown.

One entry point, `resolve_all()`. It takes the paths a caller named, translates them
across the WSL boundary, and either returns them resolved or raises with every reason it
would not. The layers and their order are ADR-0006; `docs/AGENTS.md` describes them as a
user experiences them.

Two things here are easy to get wrong and expensive to find:

**Resolve before checking, not after.** Layer 1 calls `realpath` on the candidate *and*
on each root before comparing them. A symlink inside a root pointing outside it passes a
prefix check on the path as written and fails one on the path as resolved -- that
ordering is the whole of the symlink defence, not an optimisation of it.

**A layer that cannot fire is worse than absent**, because it is trusted. Two here have a
silent-disable failure mode: layer 3 reads its globs from a file that may not be there,
and layer 4 shells out to a git that may not be installed. Both raise `PathPolicyError`
rather than defaulting to "nothing matched", because "nothing matched" is
indistinguishable from a clean pass in every log and every test.

**Validating a path and opening it are one operation, not two.** `resolve_all` returns
strings, and a handler that opens one of them later has checked a name and used a file --
two things that are only the same while nothing changes in between. `open_resolved` is the
way to open anything this module approved; it opens and then proves the descriptor refers
to the path that was approved. Reaching for `open()` on a `.posix` reopens the gap, which
is why no code in this repository does (ADR-0049).

The reference implementation of server-side prefetch had no validation whatsoever and
would read a private SSH key on request.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import posixpath
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import Config
from .wsl import UntranslatablePath, to_posix

# Layer 0 is not one of the four. It is the boundary crossing itself, named separately so
# a refusal that never reached the policy does not claim a layer that never ran.
LAYER_FORM = 0
LAYER_ROOTS = 1
LAYER_EXT = 2
LAYER_SECRET = 3
LAYER_GITIGNORE = 4
# Also not one of the four. Layers 1 to 4 decide whether a *path* may be shown; this one
# decides whether the file that opened is still at that path. It is a re-proof of the four
# rather than a fifth filter, and it runs at open time instead of resolve time, which is
# the whole reason it exists (ADR-0049).
LAYER_OPENED = 5

LAYER_NAMES = {
    LAYER_FORM: "path form",
    LAYER_ROOTS: "workspace roots",
    LAYER_EXT: "extension allowlist",
    LAYER_SECRET: "secret denylist",
    LAYER_GITIGNORE: "gitignore",
    LAYER_OPENED: "the opened file",
}


class PathPolicyError(RuntimeError):
    """The policy itself cannot be applied -- a missing globs file, an absent git.

    Not a refusal. A refusal means the policy ran and said no; this means it did not run,
    and treating the two alike is how a layer stops working without anyone noticing.
    """


@dataclass(frozen=True, slots=True)
class Refusal:
    """One path the policy will not allow, and enough to fix it in one round trip."""

    given: str  # exactly as the caller wrote it, so the message is greppable
    layer: int
    reason: str
    remedy: str

    @property
    def layer_name(self) -> str:
        return LAYER_NAMES[self.layer]

    def __str__(self) -> str:
        where = (
            self.layer_name
            if self.layer == LAYER_FORM
            else f"layer {self.layer}, {self.layer_name}"
        )
        return f"{self.given}\n    {where}: {self.reason}\n    {self.remedy}"


class PathRefused(Exception):
    """At least one path was refused, so the whole call is refused.

    Every refusal is carried, not just the first. A caller that learns about one path per
    round trip pays a full dispatch for each, which for a five-file review is five
    refusals discovered one at a time.
    """

    def __init__(
        self, refusals: Iterable[Refusal], total: int, *, surface: str = "files[]"
    ) -> None:
        self.refusals = tuple(refusals)
        self.total = total
        self.surface = surface
        head = (
            f"{len(self.refusals)} of {total} path(s) in {surface} were refused, so nothing "
            "was sent to the model. Every refusal is listed, not just the first, so one "
            "correction fixes all of them:"
        ) if total > 1 or surface == "files[]" else (
            f"The {surface} was refused, so nothing was sent to the model:"
        )
        super().__init__(head + "\n\n" + "\n\n".join(f"  {r}" for r in self.refusals))


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A path that survived all four layers."""

    given: str  # as the caller wrote it; used in messages and nowhere else
    posix: str  # real, symlink-resolved, POSIX

    @property
    def ext(self) -> str:
        """Lowercased suffix, or empty. What `Config.estimate_tokens` costs by."""
        return posixpath.splitext(self.posix)[1].lower()


# ---- layer 1 ---------------------------------------------------------------------


def resolved_roots(cfg: Config) -> tuple[str, ...]:
    """The configured roots, translated and resolved once per call.

    Roots are documented as written in native host form, so they are translated too: an
    operator who sets a root to a Windows path gets the root they meant rather than one
    that silently matches nothing.
    """
    return tuple(os.path.realpath(to_posix(r)) for r in cfg.workspace_roots)


def resolved_workdir_roots(cfg: Config) -> tuple[str, ...]:
    """The roots a `workdir` may sit in, which is a separate surface from the files read.

    `workdir_roots` empty means reuse `workspace_roots`, so the common case needs no second
    setting. They are separable because binding a directory read-write into a sandbox is a
    bigger grant than reading a file out of it: an operator may want a model able to *read*
    three projects while only ever *working* in one.
    """
    return tuple(
        os.path.realpath(to_posix(r)) for r in cfg.effective_workdir_roots
    )


def resolved_agent_bind_roots(cfg: Config) -> tuple[str, ...]:
    """The roots an agent file's `extra_binds` may resolve inside. Empty means none.

    No `to_posix` here, unlike the two above, and that is the difference rather than an
    omission: a bind is consumed by bwrap on the far side of the WSL boundary, so it is
    already POSIX and translating it would be translating something that never crossed.

    No fallback either. `workdir_roots` falls back to `workspace_roots` because both govern
    the same tree from different angles; this one governs mounts, and sharing a list with a
    reading tool is how widening one would silently widen the other.
    """
    return tuple(os.path.realpath(r) for r in cfg.agent_bind_roots)


def _within(real: str, root: str) -> bool:
    root = root.rstrip("/")
    return real == root or real.startswith(root + "/")


def path_within_roots(real: str, roots: Sequence[str]) -> bool:
    """`_within` against any of `roots`, for callers outside this module.

    A public name rather than a reached-across underscore: `agents.py` needs exactly this
    containment rule and needs it to be the *same* rule, because two modules disagreeing
    about what "inside a root" means is the failure the single definition prevents.
    """
    return any(_within(real, root) for root in roots)


def _check_roots(given: str, real: str, roots: Sequence[str]) -> Refusal | None:
    if any(_within(real, root) for root in roots):
        return None
    return Refusal(
        given=given,
        layer=LAYER_ROOTS,
        reason=f"its real location {real} is outside every workspace root.",
        remedy=(
            f"Configured roots: {', '.join(roots) or '(none)'}. If the path went through "
            "a symlink it is the resolved location that is checked, not the link. Name a "
            "file inside a root, or add the project to DELEGATE_WORKSPACE_ROOTS."
        ),
    )


# ---- layer 2 ---------------------------------------------------------------------


def _check_ext(cfg: Config, given: str, real: str) -> Refusal | None:
    """Suffix first, then whole filename.

    `config.ext_allowlist` mixes true suffixes (`.py`) with whole filenames written with
    a leading dot (`.gitignore`, `.makefile`, `.dockerfile`). `Path(".gitignore").suffix`
    is the empty string -- Python reads a leading dot as the start of the name, not as a
    separator -- so suffix matching alone refuses exactly the entries somebody went to
    the trouble of adding. A file with no suffix is therefore matched by name as well.

    That widens the allowlist slightly: a file named literally `py`, with no extension at
    all, matches the `.py` entry. Accepted -- the alternative is a second list recording
    which entries are filenames, and a second list is the drift this project exists to
    avoid. Layers 3 and 4 still apply to it.
    """
    allow = {e.lower() for e in cfg.ext_allowlist}
    name = posixpath.basename(real).lower()
    suffix = posixpath.splitext(name)[1]

    if suffix and suffix in allow:
        return None
    if not suffix and (name in allow or "." + name in allow):
        return None

    what = f"the extension {suffix!r}" if suffix else f"the filename {name!r}"
    return Refusal(
        given=given,
        layer=LAYER_EXT,
        reason=f"{what} is not on the extension allowlist.",
        remedy=(
            "Extension is the one axis that can be allowlisted for file contents -- you "
            "cannot enumerate every source file you might delegate. Add it to "
            "DELEGATE_EXT_ALLOWLIST if it is genuinely source, or paste the relevant "
            "part into the task instead."
        ),
    )


# ---- layer 3 ---------------------------------------------------------------------


def load_secret_globs(cfg: Config) -> tuple[str, ...]:
    """Read the denylist, or refuse to run without it.

    Shared with the docs gate so there is one list rather than two that drift, which is
    also why a missing file is fatal here: the gate would keep passing while the server
    quietly stopped denying anything, and nothing would report the difference.
    """
    path = Path(cfg.secret_globs_file)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PathPolicyError(
            f"Layer 3 cannot run: the secret denylist at {path} is unreadable ({e}). It "
            "is not skipped when absent -- a denylist that matches nothing is "
            "indistinguishable from one that passed. Set DELEGATE_SECRET_GLOBS_FILE, or "
            "start the server with its working directory at the repository root."
        ) from e

    globs = tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not globs:
        raise PathPolicyError(
            f"Layer 3 cannot run: {path} contains no patterns. See the note above about "
            "a denylist that matches nothing."
        )
    return globs


def _path_suffixes(real: str) -> tuple[str, ...]:
    """Every trailing run of components, plus the absolute path itself.

    Matching only the basename and the full path -- the obvious reading -- leaves the
    directory globs unable to fire at all: `.git/**` matches neither `config` nor
    `/mnt/c/proj/.git/config`. It has to be checked against `.git/config`, which is a
    suffix of the path and neither of the two. The denylist carries five such entries, so
    half of layer 3 would have been decoration.
    """
    parts = real.lstrip("/").split("/")
    return tuple(["/".join(parts[i:]) for i in range(len(parts))] + [real])


def secret_match(real: str, globs: Sequence[str]) -> str | None:
    """The first denylist pattern matching `real`, or None.

    Split out from `_check_secret` so the sandbox can ask the same question. `sandbox.py`
    shadows denylist matches at the mount level, and doing that against a second matcher
    would mean one list read two ways: a pattern could deny `read_file` while leaving the
    same file readable from a shell, and nothing would report the disagreement. The
    denylist file was already shared; this shares the reading of it.

    Returns the pattern rather than a bool because both callers need to name it -- one in
    a refusal the model reads, one in a diagnostic about what was covered up.
    """
    candidates = _path_suffixes(real.lower())
    for glob in globs:
        # fnmatchcase on pre-lowered strings, never fnmatch: fnmatch folds case through
        # os.path.normcase, a no-op on Linux and case-folding on Windows, so the same
        # denylist would behave differently on either side of the boundary.
        lowered = glob.lower()
        if any(fnmatch.fnmatchcase(c, lowered) for c in candidates):
            return glob
    return None


def _check_secret(given: str, real: str, globs: Sequence[str]) -> Refusal | None:
    glob = secret_match(real, globs)
    if glob is None:
        return None
    return Refusal(
        given=given,
        layer=LAYER_SECRET,
        reason=f"it matches the secret denylist pattern {glob!r}.",
        remedy=(
            "Delegated models never receive credential material. This list is shared "
            "with the git secrets gate, so an entry here also means the file must never "
            "be committed. Drop it from files[]."
        ),
    )


# ---- layer 4 ---------------------------------------------------------------------


def _git(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run git, turning an absent git into a policy error rather than a silent pass."""
    try:
        return subprocess.run(args, input=stdin, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise PathPolicyError(
            "Layer 4 cannot run: git is not on PATH. It is not skipped when absent -- "
            "'git found nothing ignored' and 'git never ran' are the same empty result. "
            "Install git, or set DELEGATE_RESPECT_GITIGNORE=false to drop the layer "
            "deliberately."
        ) from e


def _repo_top(directory: str) -> str | None:
    """The work tree containing `directory`, or None if it is not in one."""
    proc = _git(["git", "-C", directory, "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None  # exit 128: outside any repository. Not an error, just not ignored.
    return proc.stdout.decode("utf-8", "replace").strip() or None


def repo_status(directories: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """`git status --porcelain` for each work tree containing one of `directories`.

    Ground truth for the report a context-overflow abort produces. The point is not to be
    helpful about git: it is that an aborted delegation's own account of what it changed
    is the least trustworthy thing in the room, and this is the one statement about the
    working tree that does not come from the model (ADR-0007).

    Scoped to the directories handed in -- in practice the parents of files the delegation
    actually wrote -- and never to the whole workspace. A report that enumerated every
    root would disclose unrelated in-flight work to whoever reads the failure, and would
    cost a subprocess per root to do it.

    This is server-side git, exactly as layer 4 already uses it, and not a route into the
    sandbox: `run_bash` is a different mechanism with a different threat model (ADR-0010).
    A failure here is swallowed to an empty entry, because a report that cannot be
    produced must not replace the abort that prompted it.
    """
    tops: dict[str, None] = {}
    for directory in directories:
        try:
            top = _repo_top(directory)
        except PathPolicyError:
            return {}  # git absent. The abort still stands; it just carries no ground truth.
        if top:
            tops[top] = None
    out: dict[str, tuple[str, ...]] = {}
    for top in tops:
        proc = _git(["git", "-C", top, "status", "--porcelain"])
        if proc.returncode != 0:
            continue
        lines = proc.stdout.decode("utf-8", "replace").splitlines()
        out[top] = tuple(line for line in lines if line.strip())
    return out


def gitignored(reals: Sequence[str]) -> set[str]:
    """Which of `reals` git ignores. One `check-ignore` per repository, not per file.

    A subprocess per path is the obvious implementation and spawns a process for every
    file in a review. `--stdin -z` takes the whole set at once; the paths are grouped by
    work tree first, because a call has no reason to stay inside one project now that the
    roots enumerate three.

    Measured rather than assumed: `check-ignore` exits 1 with empty output when nothing
    is ignored and 128 outside a repository, and by design does *not* report a tracked
    file even when a pattern matches it -- which is the wanted behaviour, since a
    committed file is not ignored in any sense the caller cares about.
    """
    if not reals:
        return set()

    tops: dict[str, str | None] = {}
    by_repo: dict[str, list[str]] = defaultdict(list)
    for real in reals:
        directory = posixpath.dirname(real)
        if directory not in tops:
            tops[directory] = _repo_top(directory)
        top = tops[directory]
        if top is not None:
            by_repo[top].append(real)

    ignored: set[str] = set()
    for top, group in by_repo.items():
        proc = _git(
            ["git", "-C", top, "check-ignore", "--stdin", "-z"],
            stdin=b"\0".join(p.encode("utf-8") for p in group) + b"\0",
        )
        if proc.returncode not in (0, 1):
            continue  # 128 and anything else: treated as not-ignored, never as fatal
        ignored.update(
            chunk.decode("utf-8", "replace") for chunk in proc.stdout.split(b"\0") if chunk
        )
    return ignored


# ---- the entry point -------------------------------------------------------------


def _resolve_one(
    cfg: Config, raw: str, roots: Sequence[str], globs: Sequence[str],
    must_exist: bool = True,
) -> ResolvedPath | Refusal:
    """One path through layers 0 to 3. Layer 4 is batched and applied by the caller."""
    try:
        posix = to_posix(raw)
    except UntranslatablePath as e:
        return Refusal(
            given=raw,
            layer=LAYER_FORM,
            reason=str(e),
            remedy="Name the file by a path this server can open.",
        )

    if not posixpath.isabs(posix):
        return Refusal(
            given=raw,
            layer=LAYER_ROOTS,
            reason="it is a relative path.",
            remedy=(
                "A relative path would resolve against the server's working directory, "
                "which is not yours and is stated nowhere. Give the absolute path."
            ),
        )

    real = os.path.realpath(posix)

    # Straight-line and in order, because the order is the decision (ADR-0006) and a list
    # of checks to iterate would hide it. Existence sits *after* layer 1 deliberately:
    # checking it first would answer "does this file exist" for paths outside every root,
    # which is a small oracle the caller has no business being handed.
    refusal = _check_roots(raw, real, roots)
    if refusal is None:
        refusal = _check_exists(raw, real, must_exist)
    if refusal is None:
        refusal = _check_ext(cfg, raw, real)
    if refusal is None:
        refusal = _check_secret(raw, real, globs)
    return refusal or ResolvedPath(given=raw, posix=real)


def _check_exists(given: str, real: str, must_exist: bool = True) -> Refusal | None:
    """Layer 1's existence half.

    `must_exist=False` is for `write_file`, which creates. It relaxes *only* the missing-file
    branch: the containing directory must still be there, and an existing directory is still
    refused. Nothing else is loosened -- roots, extension and the secret denylist all still
    run, because writing to a secret path is worse than reading one, not better.
    """
    if not os.path.exists(real):
        if must_exist:
            return Refusal(
                given=given,
                layer=LAYER_ROOTS,
                reason="no such file.",
                remedy=(
                    "It resolved inside a workspace root, so the root is right and the rest "
                    f"is not: {real}"
                ),
            )
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            return Refusal(
                given=given,
                layer=LAYER_ROOTS,
                reason="the directory to write into does not exist.",
                remedy=(
                    "Writing a file does not create the tree above it, so a typo in a "
                    f"directory name would otherwise appear as a new one: {parent}"
                ),
            )
        return None
    if not os.path.isfile(real):
        return Refusal(
            given=given,
            layer=LAYER_ROOTS,
            reason="it is a directory, not a file.",
            remedy="Name the file itself. A directory is not a thing to read or write.",
        )
    return None


def resolve_search_root(cfg: Config, given: str) -> str:
    """Layer 1 and 2 for a directory a search will walk. Returns it resolved, or refuses.

    A third entry point rather than a reuse, and the reason is which roots apply.
    `_one_path` refuses a directory outright, correctly -- a directory is not a thing to
    read. `resolve_workdir` does check a directory, but against `workdir_roots`, which
    governs a read-write bind into a sandbox and is deliberately separable from the files a
    delegation may read. Searching is reading, so this checks `workspace_roots`.

    Only these two layers apply here, for the same reason they are the only two that apply
    to a workdir: the extension allowlist, the secret denylist and the gitignore check are
    about file *contents*, and they are applied per candidate by `resolve_permitted` once
    the walk has enumerated them. A directory passing this is not permission to read
    anything inside it.

    **Resolved before it is compared.** A symlink inside a root pointing out of it is a
    real escape and is invisible to any check that compares the path as written.
    """
    posix = to_posix(given)
    if not posixpath.isabs(posix):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_FORM,
            reason="it is not an absolute path.",
            remedy=(
                "The server has its own working directory and will not share yours. Give "
                "an absolute path, or omit it to search every workspace root."
            ),
        )], 1)

    real = os.path.realpath(posix)
    if not os.path.exists(real):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_FORM,
            reason=f"its real location {real} does not exist.",
            remedy="Name an existing directory or file, or omit it to search everywhere.",
        )], 1)

    roots = resolved_roots(cfg)
    refusal = _check_roots(given, real, roots)
    if refusal is not None:
        raise PathRefused([refusal], 1)
    return real


def resolve_workdir(cfg: Config, given: str) -> str:
    """Layer 1 for the `workdir` argument. Returns the resolved POSIX path, or refuses.

    Only layer 1 applies. Layers 2 to 4 are about *file contents* -- an extension allowlist,
    a secret denylist, a gitignore check -- and a working directory is not a file. The
    denylist still reaches inside it, but at the mount level rather than here, because a
    shell can read anything visible to it and a path check the shell never consults is
    decoration (ADR-0035).

    **Resolved before it is compared, which is the whole point.** A symlink inside a root
    pointing outside it is a real escape, and it is invisible to any check that compares the
    path as written. `os.path.realpath` first, then the root test -- the same order, and the
    same reason, as `_resolve_one`.

    Raises `PathRefused` carrying one refusal, so a caller that already handles the files[]
    case handles this one without learning a second exception.
    """
    posix = to_posix(given)
    if not posixpath.isabs(posix):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_FORM,
            reason="it is not an absolute path.",
            remedy=(
                "A workdir is resolved by the server, which has its own working directory "
                "and will not share yours. Give an absolute path."
            ),
        )], 1, surface="workdir")

    real = os.path.realpath(posix)
    if not os.path.isdir(real):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_FORM,
            reason=(
                f"its real location {real} is not a directory."
                if os.path.exists(real)
                else f"its real location {real} does not exist."
            ),
            remedy="A workdir must be an existing directory.",
        )], 1, surface="workdir")

    roots = resolved_workdir_roots(cfg)
    if not any(_within(real, root) for root in roots):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_ROOTS,
            reason=f"its real location {real} is outside every workdir root.",
            remedy=(
                f"Permitted workdir roots: {', '.join(roots) or '(none)'}. The resolved "
                "location is what is checked, so a symlink inside a root that points out "
                "of it is refused on where it lands, not on where it sits. Set "
                "DELEGATE_WORKDIR_ROOTS, or DELEGATE_WORKSPACE_ROOTS which it falls back to."
            ),
        )], 1, surface="workdir")
    return real


def resolve_all(
    cfg: Config, given: Sequence[str], *, must_exist: bool = True
) -> tuple[ResolvedPath, ...]:
    r"""Resolve every caller path, or raise `PathRefused` naming each one that failed.

    Returns in caller order, deduplicated by resolved path -- `C:/p/a.py` and `C:\p\a.py`
    are one file, and inlining it twice pays for it twice. Ordering for the prompt is
    `context.py`'s job (ADR-0011), not this module's; sorting here would hide a
    caller-order dependency rather than let the test for it fail.

    All-or-nothing, and that is the point for anything the caller *named*: a delegation
    that asked for six files and silently got five reads exactly like one that got six.
    `resolve_permitted` is the other disposition, for paths nobody named.
    """
    survivors, refusals = _resolve_many(cfg, given, must_exist)
    if refusals:
        raise PathRefused(list(refusals), total=len(given))
    return survivors


def resolve_permitted(
    cfg: Config, given: Sequence[str], *, must_exist: bool = True
) -> tuple[ResolvedPath, ...]:
    """The subset the policy allows. The rest are dropped, not refused.

    For paths the caller never named -- the candidates a *search* enumerates, where a file
    the policy declines is not an error but simply not a result. Refusing the whole call
    because a walk happened to pass a gitignored file would make the tool unusable, and
    reporting a hundred refusals to the model would drown the matches it asked for.

    **Never use this for a path a caller supplied.** There it is a silent drop, which is
    the failure `resolve_all` exists to prevent; that asymmetry is the whole reason these
    are two functions over one policy rather than a flag on one.

    Both go through `_resolve_many`, so the four layers are implemented once. A second
    matcher here is how a pattern would come to deny `read_file` while leaving the same
    file findable by search, with nothing reporting the disagreement.
    """
    survivors, _ = _resolve_many(cfg, given, must_exist)
    return survivors


def _resolve_many(
    cfg: Config, given: Sequence[str], must_exist: bool
) -> tuple[tuple[ResolvedPath, ...], list[Refusal]]:
    """The four layers over a list, raising nothing. Both dispositions share this."""
    if not given:
        return (), []

    roots = resolved_roots(cfg)
    globs = load_secret_globs(cfg)

    refusals: list[Refusal] = []
    survivors: list[ResolvedPath] = []
    seen: set[str] = set()

    for raw in given:
        outcome = _resolve_one(cfg, raw, roots, globs, must_exist)
        if isinstance(outcome, Refusal):
            refusals.append(outcome)
        elif outcome.posix not in seen:
            seen.add(outcome.posix)
            survivors.append(outcome)

    if cfg.respect_gitignore and survivors:
        ignored = gitignored([s.posix for s in survivors])
        kept: list[ResolvedPath] = []
        for survivor in survivors:
            if survivor.posix not in ignored:
                kept.append(survivor)
                continue
            refusals.append(
                Refusal(
                    given=survivor.given,
                    layer=LAYER_GITIGNORE,
                    reason="git ignores it.",
                    remedy=(
                        "Ignored files are build output, local environment or vendored "
                        "bulk far more often than they are the thing under review. If "
                        "this one really is source, commit it or set "
                        "DELEGATE_RESPECT_GITIGNORE=false."
                    ),
                )
            )
        survivors = kept

    return tuple(survivors), refusals


# ---- opening what was resolved -----------------------------------------------------

# Absent on Windows, where both are 0 and fold out of the flag word. That platform has no
# `run_bash` -- bubblewrap is not there, so `available_tool_names` subtracts it -- and so
# has no in-sandbox adversary to race us. It also refuses to unlink a file while a
# descriptor is open, measured, so the post-open half of the attack cannot be staged there
# either. Stated rather than glossed: on such a platform the proof below is weaker.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)

_MODE_FLAGS = {
    "rb": os.O_RDONLY,
    "r+b": os.O_RDWR,
    # No O_TRUNC, deliberately. See `open_resolved`.
    "wb": os.O_WRONLY | os.O_CREAT,
}


@dataclass(frozen=True, slots=True)
class OpenedFile:
    """A descriptor on a file the policy approved, proven to be that file."""

    entry: ResolvedPath
    handle: BinaryIO
    created: bool  # this call brought the file into existence; only ever true for "wb"


def _opened_path(fd: int) -> str | None:
    """Where the descriptor actually points, or `None` where the OS will not say.

    Linux answers through procfs, and it answers for DrvFs under `/mnt/c` as well as for
    ext4 -- both measured, both returning exactly what `os.path.realpath` returned for the
    path we opened. A file unlinked since the open comes back with a " (deleted)" suffix,
    which compares unequal and is therefore refused, correctly: we are holding something
    that is no longer at the approved path.
    """
    link = f"/proc/self/fd/{fd}"
    if not os.path.islink(link):
        return None
    return os.path.realpath(link)


def _prove_descriptor(entry: ResolvedPath, fd: int) -> None:
    """Refuse unless the open descriptor is the file the four layers approved.

    **What this catches is redirection, not substitution.** Every one of layers 1 to 4 is
    a function of the path, so a different *regular* file appearing at an approved path is
    not a policy bypass -- the same bytes could have arrived through `write_file`. What is
    a bypass is the path coming to name something outside the approved set: a symlink to a
    key, a swapped parent directory. That is what this compares for, and measurement
    confirms the distinction is real -- a pre-open substitution of one regular file for
    another fires neither check, by design rather than by omission.
    """
    actual = _opened_path(fd)
    if actual is not None:
        if actual == entry.posix:
            return
        raise PathRefused(
            [Refusal(
                given=entry.given,
                layer=LAYER_OPENED,
                reason=(
                    "it passed the policy, but the file that opened is somewhere else "
                    f"now: the descriptor refers to {actual}."
                ),
                remedy=(
                    "The path changed between being approved and being opened. Nothing "
                    "was read or written. Resolve it again, and if this repeats, "
                    "something else is moving that path while you work."
                ),
            )],
            1,
            surface="opened file",
        )

    # No procfs. Inode identity is what is left: it proves the path still names the file
    # we hold, which catches a swap after the open but not one before it. Weaker, and the
    # comment on _NOFOLLOW says why that is accepted here rather than papered over.
    try:
        held = os.fstat(fd)
        onpath = os.lstat(entry.posix)
    except OSError as e:
        raise PathRefused(
            [Refusal(
                given=entry.given,
                layer=LAYER_OPENED,
                reason=f"the file it opened could not be checked ({e.strerror or e}).",
                remedy="Nothing was read or written. Resolve the path again.",
            )],
            1,
            surface="opened file",
        ) from e

    if (held.st_dev, held.st_ino) != (onpath.st_dev, onpath.st_ino):
        raise PathRefused(
            [Refusal(
                given=entry.given,
                layer=LAYER_OPENED,
                reason=(
                    "it passed the policy, but the path no longer names the file that "
                    "opened."
                ),
                remedy=(
                    "The path changed between being approved and being opened. Nothing "
                    "was read or written. Resolve it again."
                ),
            )],
            1,
            surface="opened file",
        )


def open_resolved(entry: ResolvedPath, mode: str) -> OpenedFile:
    """Open a path this module approved, and prove the descriptor is that path.

    The only sanctioned way to open anything `resolve_all` or `resolve_permitted` returned.
    `mode` is `"rb"`, `"r+b"` or `"wb"`; `"r+b"` exists so a read-modify-write holds one
    descriptor for the whole operation and never opens twice against one check, which is
    what makes `edit_file` safe by construction rather than by review.

    Two things are load-bearing and neither is obvious.

    **`O_NOFOLLOW`, despite the usual objection.** The objection is that it refuses
    legitimate symlinked checkouts, and it would -- on the path a *caller* wrote. This flag
    goes on `entry.posix`, which `realpath` has already collapsed, so the final component
    is known not to be a link at the moment the policy approved it. A link there now is the
    attack, not a checkout. Measured: it refuses with ELOOP, which is translated below into
    a refusal rather than left to surface as "too many levels of symbolic links".

    **Open, prove, and only then destroy.** `"wb"` sets `O_CREAT` but never `O_TRUNC`,
    because `O_TRUNC` empties the file at open time -- before any proof can run. A checked
    truncation after the proof is a guard; a check after `O_TRUNC` is a report of what was
    already lost. `O_EXCL` is tried first so `created` can be reported without a second
    `stat` on the same string, which was itself a second use of an unvalidated path.
    """
    try:
        flags = _MODE_FLAGS[mode]
    except KeyError:
        raise ValueError(f"unsupported mode {mode!r}; use 'rb', 'r+b' or 'wb'") from None

    flags |= _NOFOLLOW | _BINARY
    created = False
    try:
        if mode == "wb":
            try:
                fd = os.open(entry.posix, flags | os.O_EXCL, 0o644)
                created = True
            except FileExistsError:
                fd = os.open(entry.posix, flags)
        else:
            fd = os.open(entry.posix, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise PathRefused(
                [Refusal(
                    given=entry.given,
                    layer=LAYER_OPENED,
                    reason=(
                        "it passed the policy as a file, and is a symbolic link now. It "
                        "was not opened."
                    ),
                    remedy=(
                        "The path changed between being approved and being opened. "
                        "Nothing was read or written. Resolve it again."
                    ),
                )],
                1,
                surface="opened file",
            ) from e
        raise

    try:
        _prove_descriptor(entry, fd)
        if mode == "wb" and not created:
            # The truncation the caller expected from "wb", moved to after the proof.
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise

    return OpenedFile(entry=entry, handle=os.fdopen(fd, mode), created=created)
