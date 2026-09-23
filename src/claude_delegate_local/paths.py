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
import glob as glob_module  # `glob` is a local name for a denylist pattern throughout
import os
import posixpath
import re
import stat as stat_module
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
        self,
        refusals: Iterable[Refusal],
        total: int,
        *,
        surface: str = "files[]",
        before_dispatch: bool = True,
    ) -> None:
        """`surface` names what was refused; `before_dispatch` says what that cost.

        Both are read by the model, which is why neither may overstate. A refusal raised
        while resolving `files[]` or a `workdir` happens before anything is sent, so the
        call is over. A refusal raised *inside* a tool call is returned to the model as an
        error result and the delegation continues -- saying "nothing was sent to the model"
        there is false, and it was, for every `read_file` and `search_files` refusal until
        2026-09-06.
        """
        self.refusals = tuple(refusals)
        self.total = total
        self.surface = surface
        self.before_dispatch = before_dispatch
        cost = ", so nothing was sent to the model" if before_dispatch else ""
        head = (
            f"{len(self.refusals)} of {total} path(s) in {surface} were refused{cost}. "
            "Every refusal is listed, not just the first, so one "
            "correction fixes all of them:"
        ) if total > 1 or surface == "files[]" else (
            f"The {surface} was refused{cost}:"
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


def extension_refusal(cfg: Config, path: str) -> Refusal | None:
    """Layer 2 for a path that need not exist, for callers outside this module.

    `read_git` judges paths in history, which may be long deleted, by the same rule
    `read_file` applies to the worktree -- one predicate, so the two cannot disagree.
    """
    return _check_ext(cfg, path, path)


# ---- layer 3 ---------------------------------------------------------------------


def resolve_configured_path(raw: str) -> Path:
    """Where a configured list file actually is: relative settings hang off the cwd.

    One copy, three callers -- `load_secret_globs`, `load_opaque_globs`, and the shadow
    scan that has to recognise both files to avoid covering them. The third is why this is
    a function at all: comparing a walked path against a setting resolved a different way
    is how the scan came to cover the very list it had just read.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def load_secret_globs(cfg: Config) -> tuple[str, ...]:
    """Read the denylist, or refuse to run without it.

    Shared with the docs gate so there is one list rather than two that drift, which is
    also why a missing file is fatal here: the gate would keep passing while the server
    quietly stopped denying anything, and nothing would report the difference.
    """
    path = resolve_configured_path(cfg.secret_globs_file)
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

    A `!` prefix exempts, and is checked before any deny. It exists because a broad
    pattern is sometimes right about a family and wrong about one tracked member of it:
    `.env.*` should deny every real environment file and was also denying `.env.example`,
    which is committed and is the readable control on that pair. Narrowing the pattern
    instead would be an allowlist by omission -- a `.env.production` added later would be
    readable, with nothing to say so -- where an exemption stays closed by default and
    names exactly what it opens.
    """
    candidates = _path_suffixes(real.lower())

    def hit(pattern: str) -> bool:
        # fnmatchcase on pre-lowered strings, never fnmatch: fnmatch folds case through
        # os.path.normcase, a no-op on Linux and case-folding on Windows, so the same
        # denylist would behave differently on either side of the boundary.
        lowered = pattern.lower()
        return any(fnmatch.fnmatchcase(c, lowered) for c in candidates)

    if any(hit(glob[1:]) for glob in globs if glob.startswith("!")):
        return None
    for glob in globs:
        if not glob.startswith("!") and hit(glob):
            return glob
    return None


# Key material, by its armour rather than by its file name. Deliberately tiny and
# deliberately anchored on strings that do not occur in prose by accident: a renamed key is
# still PEM inside, and PEM announces itself. This is **not** `scan_text` pointed at file
# contents -- that scanner hunts RFC1918 addresses, private-DNS suffixes and
# non-allowlisted emails, and would fire on the very sources a review delegation exists to
# read. Precision is the whole design goal; a check that cries wolf on source gets disabled,
# and a disabled check is worse than none because it is still believed. (ADR-0096)
#
# Both layers call this one table. `paths.py` refuses a file the model asked for and
# `sandbox.py` covers one a command could have opened, and they are independent layers
# rather than redundant ones -- but the *rule* they apply must be one rule, or a pattern
# added to one would leave the same bytes readable through the other with nothing reporting
# the disagreement. That is the mistake `secret_match` already exists to avoid.
_KEY_MARKERS: tuple[re.Pattern[bytes], ...] = (
    # PEM private keys of every flavour: RSA, EC, DSA, OPENSSH, PGP BLOCK, and the
    # unlabelled PKCS#8 form. The label is bounded so this cannot run away down a line.
    re.compile(rb"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY( BLOCK)?-----"),
    # PuTTY's own format, which is not PEM and is the common Windows-side key file.
    re.compile(rb"PuTTY-User-Key-File-\d"),
)


def key_material_marker(head: bytes) -> str | None:
    """The name of the first key-material marker in `head`, or None.

    Bytes rather than text, and never decoded: a key file may be any encoding or none, and
    a decode that raised would turn a detector into a crash. The caller supplies however
    much of the file it is willing to read, which is what bounds the cost.

    Returns the marker so both callers can name it -- one in a refusal the model reads, one
    in a diagnostic about what was covered up -- exactly as `secret_match` returns its
    pattern.
    """
    for pattern in _KEY_MARKERS:
        found = pattern.search(head)
        if found is not None:
            return found.group(0).decode("ascii", "replace")
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
    # Fed or closed, never inherited: the server's own stdin is the MCP stream.
    feed: dict[str, object] = (
        {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    )
    # The C locale, because `_repo_top` tells "not a repository" from every other failure
    # by git's own words, and a translated git would say them differently.
    env = {**os.environ, "LC_ALL": "C"}
    try:
        return subprocess.run(args, capture_output=True, check=False, env=env, **feed)
    except FileNotFoundError as e:
        raise PathPolicyError(
            "Layer 4 cannot run: git is not on PATH. It is not skipped when absent -- "
            "'git found nothing ignored' and 'git never ran' are the same empty result. "
            "Install git, or set DELEGATE_RESPECT_GITIGNORE=false to drop the layer "
            "deliberately."
        ) from e


# The one failure that is an answer: git found no repository. Measured in both of its
# shapes, "(or any of the parent directories)" and "(or any parent up to mount point ...)".
_NOT_A_REPOSITORY = "fatal: not a git repository"


def _layer4_failed(what: str, proc: subprocess.CompletedProcess[bytes]) -> PathPolicyError:
    err = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
    return PathPolicyError(
        f"Layer 4 cannot run: git could not {what} ({err}). It is not read as 'nothing "
        "ignored' -- a layer that cannot answer refuses. Repair the repository, or set "
        "DELEGATE_RESPECT_GITIGNORE=false to drop the layer deliberately."
    )


def _repo_top(directory: str) -> str | None:
    """The work tree containing `directory`, or None if it is not in one."""
    proc = _git(["git", "-C", directory, "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        if proc.stderr.decode("utf-8", "replace").startswith(_NOT_A_REPOSITORY):
            return None  # outside any repository: not an error, just not ignored
        raise _layer4_failed(f"find the repository holding {directory}", proc)
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


def gitignored(
    reals: Sequence[str], *, tops: dict[str, str | None] | None = None
) -> set[str]:
    """Which of `reals` git ignores. One `check-ignore` per repository, not per file.

    A subprocess per path is the obvious implementation and spawns a process for every
    file in a review. `--stdin -z` takes the whole set at once; the paths are grouped by
    work tree first, because a call has no reason to stay inside one project now that the
    roots enumerate three.

    Measured rather than assumed: `check-ignore` exits 1 with empty output when nothing
    is ignored and 128 outside a repository, and by design does *not* report a tracked
    file even when a pattern matches it -- which is the wanted behaviour, since a
    committed file is not ignored in any sense the caller cares about.

    **`check-ignore` is not the only subprocess here, and the other one is per directory.**
    Finding the work tree costs a `rev-parse` for each distinct parent, so 2,000 candidates
    over 389 directories is 389 subprocesses before a single `check-ignore` runs -- 20.5s of
    a 140.9s policy pass, profiled 2026-09-15 and mis-attributed to `check-ignore` itself.
    `tops` is how a caller that asks repeatedly pays that once: hand the same dict to every
    call and a second question about a directory already seen costs nothing. Omitted, the
    behaviour is what it always was.
    """
    if not reals:
        return set()

    if tops is None:
        tops = {}
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
            raise _layer4_failed(f"read what {top} ignores", proc)
        ignored.update(
            chunk.decode("utf-8", "replace") for chunk in proc.stdout.split(b"\0") if chunk
        )
    return ignored


# ---- the entry point -------------------------------------------------------------


def _realpath(posix: str, prefixes: dict[str, str]) -> str:
    """`os.path.realpath`, with the directory above `posix` remembered across a batch.

    A batch is candidates from one walk, so they share deep prefixes and the kernel re-walks
    the same directories once per candidate: 13.5 `lstat` each over 2,000 of them, 57% of the
    policy (JOURNAL 2026-09-15). Remembering the parent collapses that to one walk per
    distinct directory -- measured 30.40s to 4.47s over 2,000 candidates, 0.858s to 0.019s
    over the 147 a pruned walk now produces.

    **The final component is resolved every time and is never cached.** That is the whole
    safety argument, not a detail: `realpath` resolves a symlink in the last segment too, so
    a cache covering the whole path would hand layer 1 the inside-the-root spelling of a link
    pointing out of it -- the single check it exists to fail. `readlink` answers "is this
    last segment a link" in one syscall, and only a path that really is one pays a full walk.

    The cache lives for one `_resolve_many` call and is never shared between calls, so a
    prefix is re-read the next time anyone asks. `resolved_roots` and `load_secret_globs`
    already refresh per call for the same reason.
    """
    directory, name = posixpath.split(posix)
    if not name or name in (".", "..") or not directory:
        # Not a plain parent/child split, so there is no prefix to reuse. Rare enough that
        # handling it properly is cheaper than reasoning about what a cache would mean.
        return os.path.realpath(posix)

    real_dir = prefixes.get(directory)
    if real_dir is None:
        real_dir = os.path.realpath(directory)
        prefixes[directory] = real_dir

    candidate = posixpath.join(real_dir, name)
    try:
        os.readlink(candidate)
    except OSError:
        return candidate  # not a symlink, or not there at all: already fully resolved
    return os.path.realpath(candidate)


@dataclass(frozen=True)
class _Batch:
    """What every candidate in one `_resolve_many` call shares.

    Bundled rather than passed one by one because they have a single lifetime, and that is
    the point: `roots` and `globs` are deliberately re-read on every call, and `prefixes`
    must not outlive them. One object makes that hard to get wrong by accident.
    """

    roots: Sequence[str]
    globs: Sequence[str]
    prefixes: dict[str, str]


def _resolve_one(
    cfg: Config, raw: str, batch: _Batch, must_exist: bool = True
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

    real = _realpath(posix, batch.prefixes)

    # Straight-line and in order, because the order is the decision (ADR-0006) and a list
    # of checks to iterate would hide it. Existence sits *after* layer 1 deliberately:
    # checking it first would answer "does this file exist" for paths outside every root,
    # which is a small oracle the caller has no business being handed.
    refusal = _check_roots(raw, real, batch.roots)
    if refusal is None:
        refusal = _check_exists(raw, real, must_exist)
    if refusal is None:
        refusal = _check_ext(cfg, raw, real)
    if refusal is None:
        refusal = _check_secret(raw, real, batch.globs)
    return refusal or ResolvedPath(given=raw, posix=real)


def _check_exists(given: str, real: str, must_exist: bool = True) -> Refusal | None:
    """Layer 1's existence half.

    `must_exist=False` is for `write_file`, which creates. It relaxes *only* the missing-file
    branch: the containing directory must still be there, and an existing directory is still
    refused. Nothing else is loosened -- roots, extension and the secret denylist all still
    run, because writing to a secret path is worse than reading one, not better.

    One `stat`, not two. `exists` then `isfile` asks the filesystem the same question twice
    and throws the first answer away -- 37.1s over 4,000 calls for 2,000 candidates, profiled
    2026-09-15. A single `stat` carries both facts, and the mode says which.
    """
    try:
        st: os.stat_result | None = os.stat(real)
    except OSError:
        st = None  # missing, a broken link, or unreadable: `exists` was false for all three

    if st is None:
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
    if not stat_module.S_ISREG(st.st_mode):
        return Refusal(
            given=given,
            layer=LAYER_ROOTS,
            reason="it is a directory, not a file.",
            remedy="Name the file itself. A directory is not a thing to read or write.",
        )
    return None


def resolve_search_root(
    cfg: Config, given: str, *, surface: str = "files[]", before_dispatch: bool = True
) -> str:
    """Path form and layer 1 for a directory a search will walk. Resolved, or refused.

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
    # Both remedies below name the roots rather than offering to drop `path`. Measured
    # 2026-09-13: a delegation guessed a root, was refused, took the old advice to "omit it
    # to search everywhere", and spent 239s on the retry. A refusal fires exactly when the
    # model has shown it does not know the layout, which is the worst moment to recommend
    # walking every root -- and the roots are the one thing it was missing. (ADR-0074)
    roots = ", ".join(resolved_roots(cfg)) or "(none configured)"

    posix = to_posix(given)
    if not posixpath.isabs(posix):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_FORM,
            reason="it is not an absolute path.",
            remedy=(
                "The server has its own working directory and will not share yours. Give "
                f"an absolute path under one of these roots: {roots}"
            ),
        )], 1, surface=surface, before_dispatch=before_dispatch)

    real = os.path.realpath(posix)
    # Roots before existence, as in `_resolve_one`: the other order tells a caller whether
    # a path outside every root exists.
    refusal = _check_roots(given, real, resolved_roots(cfg))
    if refusal is not None:
        raise PathRefused([refusal], 1, surface=surface, before_dispatch=before_dispatch)
    if not os.path.exists(real):
        raise PathRefused([Refusal(
            given=given,
            layer=LAYER_FORM,
            reason=f"its real location {real} does not exist.",
            remedy=(
                "Name an existing directory or file under one of these roots, narrowing to "
                f"a subdirectory where you can: {roots}"
            ),
        )], 1, surface=surface, before_dispatch=before_dispatch)
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
    # Roots before existence, as in `_resolve_one`: the other order tells a caller whether
    # a path outside every root exists.
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
    return real


def resolve_files(
    cfg: Config,
    given: Sequence[str],
    *,
    must_exist: bool = True,
) -> tuple[tuple[ResolvedPath, ...], tuple[Refusal, ...]]:
    """Resolve every caller path, returning what survived beside what did not.

    The non-raising disposition, and `files[]` is the surface that wants it: a prefetch of
    a dozen files has eleven useful answers left when one path is wrong, and discarding
    them costs the caller a round trip to learn something the reply could have carried.
    ADR-0061.

    `resolve_all` is this plus a raise, and stays right wherever one path means one answer
    -- a mid-loop `read_file` has nothing to partially succeed at, so `tools.py` keeps it.

    Splitting here rather than catching `PathRefused` in the caller keeps the refusals as
    data. An exception carrying a list the handler must take apart is a return value that
    has to be caught first.
    """
    survivors, refusals = _resolve_many(cfg, given, must_exist)
    return survivors, tuple(refusals)


# A pattern is anything carrying one of these. Deliberately the same three `fnmatch` and
# `glob` agree on, so "does this look like a pattern" and "what does it match" cannot
# disagree -- a path containing a literal bracket is the case where they would, and it is
# rarer than the shorthand is useful.
_MAGIC = re.compile(r"[*?\[]")


def has_magic(pattern: str) -> bool:
    """Whether this string is a glob rather than a path."""
    return _MAGIC.search(pattern) is not None


def _glob_anchor(pattern: str) -> str:
    """The longest leading run of components with no magic in them.

    What the expansion is allowed to start from, and the whole reason expansion is bounded:
    `glob` walks from the first magic component down, so checking the anchor against the
    workspace roots *before* walking is what stops `/**/*.py` reading the machine. Checking
    afterwards would be checking the results of a walk that had already happened.
    """
    parts = pattern.split("/")
    keep: list[str] = []
    for part in parts[:-1]:
        if has_magic(part):
            break
        keep.append(part)
    return "/".join(keep) or "/"


def expand_globs(
    cfg: Config, given: Sequence[str]
) -> tuple[tuple[str, ...], tuple[Refusal, ...]]:
    """Turn any pattern in `given` into the paths it matches. Literals pass through.

    A shorthand for naming many files, never a way to *look* for anything -- `search_files`
    is the tool that looks, and it takes a required `path` for reasons this would undo if
    it grew a recursive default. Expansion happens here, before resolution, so every match
    then goes through the same four layers a hand-written path does: this function widens
    what a caller may *name*, and widens nothing about what the policy allows.

    Matches are files only. A directory is a legitimate glob match and never a legitimate
    prefetch, and letting one through would spend a refusal per directory to say so.

    Sorted, because `glob` returns directory order -- which is arbitrary, differs between
    machines, and would make the prompt's file block unstable for no reason. The prefetch
    sorts again for its own budget; this sort is about which files a cap keeps.
    """
    roots = resolved_roots(cfg)
    kept: list[str] = []
    refusals: list[Refusal] = []
    for raw in given:
        if not has_magic(raw):
            kept.append(raw)
            continue
        outcome = _expand_one(cfg, raw, roots)
        if isinstance(outcome, Refusal):
            refusals.append(outcome)
        else:
            kept.extend(outcome)
    return tuple(kept), tuple(refusals)


def _expand_one(cfg: Config, raw: str, roots: Sequence[str]) -> list[str] | Refusal:
    """One pattern to its matches, or the refusal explaining why there are none."""
    try:
        pattern = to_posix(raw)
    except UntranslatablePath as e:
        return Refusal(
            given=raw, layer=LAYER_FORM, reason=str(e),
            remedy="Give the pattern as an absolute path with forward slashes.",
        )
    if not posixpath.isabs(pattern):
        return Refusal(
            given=raw,
            layer=LAYER_ROOTS,
            reason="it is a relative pattern, and there is no directory to relate it to.",
            remedy=(
                "A glob is absolute like every other entry in files[]: the server has no "
                "notion of your working directory. Prefix it with a workspace root."
            ),
        )
    anchor = os.path.realpath(_glob_anchor(pattern))
    if not path_within_roots(anchor, roots):
        return Refusal(
            given=raw,
            layer=LAYER_ROOTS,
            reason=f"it would expand from {anchor}, which is outside every workspace root.",
            remedy=(
                f"Configured roots: {', '.join(roots) or '(none)'}. The part of a pattern "
                "before its first wildcard has to sit inside one, or the expansion would "
                "walk the machine rather than the workspace."
            ),
        )

    cap = cfg.max_glob_matches
    # `iglob`, and stopped at the cap: `glob` builds the whole list before returning, so a
    # `**` over a large root pays for every match whether or not the cap keeps it. This is
    # the bound on the *walk*, where the cap below is the bound on the answer.
    found: list[str] = []
    for hit in glob_module.iglob(pattern, recursive=True):
        if os.path.isfile(hit):
            found.append(hit)
        if len(found) > cap:
            break
    if not found:
        return Refusal(
            given=raw,
            layer=LAYER_ROOTS,
            reason="it matched no files.",
            remedy=(
                "A pattern that matches nothing is almost always a typo, and silently "
                "contributing no files is worse than saying so. Check the directory, and "
                "remember ** needs to be its own component."
            ),
        )
    if len(found) > cap:
        return Refusal(
            given=raw,
            layer=LAYER_ROOTS,
            reason=f"it matches more than {cap} files.",
            remedy=(
                "Narrow it. Prefetching hundreds of files spends the whole token budget "
                "on the first few and reports the rest as skipped, which is a slower way "
                "of sending nothing useful. DELEGATE_MAX_GLOB_MATCHES raises the cap."
            ),
        )
    return sorted(found)


def resolve_all(
    cfg: Config,
    given: Sequence[str],
    *,
    must_exist: bool = True,
    surface: str = "files[]",
    before_dispatch: bool = True,
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
    survivors, refusals = resolve_files(cfg, given, must_exist=must_exist)
    if refusals:
        raise PathRefused(
            list(refusals), total=len(given),
            surface=surface, before_dispatch=before_dispatch,
        )
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
    # One batch, one cache. See `_realpath` for why it holds prefixes and not whole paths.
    batch = _Batch(roots=roots, globs=globs, prefixes={})

    for raw in given:
        outcome = _resolve_one(cfg, raw, batch, must_exist)
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


def open_resolved(entry: ResolvedPath, mode: str, *, scan_bytes: int = 0) -> OpenedFile:
    """Open a path this module approved, and prove the descriptor is that path.

    `scan_bytes` turns on the content check: that many bytes of a read-mode file are
    scanned for key material, and a hit refuses. It defaults to *off* because this
    function has callers that are not the model -- the config reader, the agent loader --
    and a detector that fired on those would be refusing the server's own material to the
    server. The tool layer passes the configured value; the rest pass nothing. (ADR-0096)

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
        if mode == "wb":
            if not created:
                # The truncation the caller expected from "wb", moved to after the proof.
                os.ftruncate(fd, 0)
        elif scan_bytes > 0:
            _refuse_key_material(entry, fd, scan_bytes)
    except BaseException:
        os.close(fd)
        raise

    return OpenedFile(entry=entry, handle=os.fdopen(fd, mode), created=created)


def _refuse_key_material(entry: ResolvedPath, fd: int, scan_bytes: int) -> None:
    """Refuse a file whose *bytes* are key material, whatever it is called.

    Reads through the proven descriptor rather than reopening `entry.posix`, because a
    second `open` on the path is the check-then-use gap `open_resolved` exists to close
    (ADR-0049). `os.pread` leaves the file offset alone, so the handle the caller receives
    is still at byte zero and no caller has to know this ran.

    Runs *after* `_prove_descriptor` on purpose: scanning first would be scanning whatever
    the path happened to name at that moment, which is the substitution the proof rules
    out. And only on a read: a "wb" handle has already been truncated by here, so there
    would be nothing to scan, and writing a key into the workspace is not this layer's
    question.

    An unreadable descriptor is not a refusal. This is a detector sitting behind four
    layers that have already approved the path, so a read error here means the file went
    away or the kernel said no -- both of which the caller is about to discover properly.
    Refusing on it would turn a transient into a policy verdict.
    """
    try:
        head = os.pread(fd, scan_bytes, 0)
    except OSError:
        return
    marker = key_material_marker(head)
    if marker is None:
        return
    raise PathRefused(
        [Refusal(
            given=entry.given,
            layer=LAYER_SECRET,
            reason=(
                f"its contents are key material -- it begins {marker!r} -- whatever it is "
                "named. The denylist matches names, and this file's name did not match."
            ),
            remedy=(
                "Delegated models never receive credential material, and renaming a key "
                "does not make it readable. Nothing was read. If this is a fixture rather "
                "than a real key, it still cannot be sent: quote the shape you need "
                "instead of the file."
            ),
        )],
        1,
        surface="opened file",
    )
