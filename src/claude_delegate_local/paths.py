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

The reference implementation of server-side prefetch had no validation whatsoever and
would read a private SSH key on request.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .wsl import UntranslatablePath, to_posix

# Layer 0 is not one of the four. It is the boundary crossing itself, named separately so
# a refusal that never reached the policy does not claim a layer that never ran.
LAYER_FORM = 0
LAYER_ROOTS = 1
LAYER_EXT = 2
LAYER_SECRET = 3
LAYER_GITIGNORE = 4

LAYER_NAMES = {
    LAYER_FORM: "path form",
    LAYER_ROOTS: "workspace roots",
    LAYER_EXT: "extension allowlist",
    LAYER_SECRET: "secret denylist",
    LAYER_GITIGNORE: "gitignore",
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

    def __init__(self, refusals: Iterable[Refusal], total: int) -> None:
        self.refusals = tuple(refusals)
        self.total = total
        head = (
            f"{len(self.refusals)} of {total} path(s) in files[] were refused, so nothing "
            "was sent to the model. Every refusal is listed, not just the first, so one "
            "correction fixes all of them:"
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


def _within(real: str, root: str) -> bool:
    root = root.rstrip("/")
    return real == root or real.startswith(root + "/")


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


def resolve_all(
    cfg: Config, given: Sequence[str], *, must_exist: bool = True
) -> tuple[ResolvedPath, ...]:
    r"""Resolve every caller path, or raise `PathRefused` naming each one that failed.

    Returns in caller order, deduplicated by resolved path -- `C:/p/a.py` and `C:\p\a.py`
    are one file, and inlining it twice pays for it twice. Ordering for the prompt is
    `context.py`'s job (ADR-0011), not this module's; sorting here would hide a
    caller-order dependency rather than let the test for it fail.
    """
    if not given:
        return ()

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

    if refusals:
        raise PathRefused(refusals, total=len(given))
    return tuple(survivors)
