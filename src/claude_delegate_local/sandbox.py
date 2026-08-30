"""The bubblewrap invocation that confines `run_bash`, and the refusal when it cannot.

Bubblewrap starts from an **empty root**: nothing exists inside unless it was explicitly
bound, so the real HOME is not merely unwritable, it is absent, and `~/.ssh` with it. That
is the whole reason this module exists rather than a chmod or an allowlist of commands.

Two seams, kept apart on purpose. `build_argv` is pure -- config plus a request in, argv
out, no filesystem, no subprocess, no PATH lookup -- so the bind order that carries the
security properties can be asserted in tests on a machine with no `bwrap` installed. `run`
is the only function here that touches the world. A design where argv construction happened
inside the subprocess call would be testable only by running it, which on Windows means not
at all, and the ordering rules below are exactly the kind of thing that regresses silently.

This module and `paths.py` are **independent layers, not redundant ones** (ADR-0010).
`read_file` and `write_file` are governed by the path policy and run in the server process;
they never come here. Only `run_bash` is confined. A bug in one is not covered by the other.

The argv was corrected against a running kernel rather than against documentation, and both
corrections are load-bearing (ADR-0021). See `_BASE_ARGV` for what that means in practice.

Not a port. The ancestor logs a warning and runs the command unconfined when bwrap is
missing; here that is the one thing that never happens.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .paths import load_secret_globs, secret_match
from .wsl import to_posix

log = logging.getLogger(__name__)

# Environment names a sandboxed command may inherit. Deliberately tiny: `--clearenv` wipes
# everything, and each name here is one an operator would have to re-add by hand otherwise.
# Credentials reach a shell through the environment at least as often as through a file, so
# this list grows only through `env_passthrough`, where the widening is an operator's own
# recorded decision rather than a default nobody chose.
BUILTIN_ENV_ALLOWLIST: tuple[str, ...] = ("LANG", "LC_ALL", "TERM")

# PATH inside the sandbox. Points at the symlink targets below, not at the host's PATH,
# which names directories that do not exist inside an empty root.
SANDBOX_PATH = "/usr/bin:/usr/sbin"

# The flags that are always present, in order.
#
# `--symlink usr/lib64 /lib64` and `--symlink usr/sbin /sbin` are MANDATORY on x86-64 and
# are not a distro-specific nicety (ADR-0021). Without lib64 the ELF interpreter is absent,
# and the failure actively misleads: the kernel returns ENOENT for a missing interpreter, so
# bwrap reports "No such file or directory" against the *executable*, which is present and
# readable. Anyone debugging that message will look at the wrong file.
#
# `--unshare-all` drops every namespace including the network; `--share-net` later opts the
# network back in when, and only when, an agent asked for it. That combination was verified
# by running it, not read off a manual page.
_BASE_ARGV: tuple[str, ...] = (
    "--unshare-all",
    "--die-with-parent",
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/etc", "/etc",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/tmp",
    "--symlink", "usr/bin", "/bin",
    "--symlink", "usr/lib", "/lib",
    "--symlink", "usr/lib64", "/lib64",
    "--symlink", "usr/sbin", "/sbin",
)


class SandboxUnavailable(RuntimeError):
    """Bubblewrap is absent or unusable.

    Never caught to mean "run it unconfined". There is no configuration of this server in
    which a shell command runs outside the sandbox: a control an operator can switch off is
    still a control that can silently be off, and neither the caller nor the model can tell
    the difference from the outside (ADR-0010).
    """


class SecretShadowIncomplete(RuntimeError):
    """The secret scan hit its budget before it finished, so the command does not run.

    Fail-closed, for the same reason `load_secret_globs` refuses a missing denylist: partial
    coverage is indistinguishable from full coverage once the command is running, and the
    thing left uncovered is by definition the thing the list exists to cover.
    """


@dataclass(frozen=True, slots=True)
class ShadowTarget:
    """One denylist match, and the mount that covers it up.

    `kind` decides the primitive, and the two are not interchangeable -- a tmpfs needs a
    directory to mount on and a regular file needs something file-shaped over it. Both were
    checked against a running bwrap rather than read off documentation (ADR-0021).
    """

    path: str
    kind: str  # "dir" | "file"
    matched: str



@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """Everything one `run_bash` call needs, already resolved to POSIX absolute paths.

    Built by the caller rather than here: this module does not re-derive workspace roots,
    and does not import the tool layer's concerns. `workdir` is `None` for every caller that
    exists today -- `agents.py` (M6) is the first that will pass a real one -- but the bind
    ordering it participates in is specified and tested now, because discovering the rule
    later means discovering it from a bug.

    No timeout field: that is `cfg.run_bash_timeout`, and a default here would be a config
    default living outside `config.py`, which is the one thing that file exists to prevent.
    """

    command: str
    home: str
    workdir: str | None = None
    network: bool = False
    extra_binds: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """What the process actually did, as opposed to what the model says it did.

    `exit_code` is the server's own record of a real process exit and is the only account of
    it that anything downstream may trust (ADR-0007). `None` means the command was killed
    for exceeding its timeout, which is kept distinct from a genuine 124 that a command
    chose to return itself -- collapsing those two would make a timeout indistinguishable
    from a program reporting one.
    """

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


def probe_toolchain_binds(cfg: Config) -> tuple[str, ...]:
    """Resolve the extra read-only binds, or find `uv` when the operator named none.

    An operator's own list wins outright and is not merged with the probe: a configured
    value that silently gained an entry nobody wrote would be the config-drift this project
    exists to avoid.

    Only the binary is bound. `uv` needs the loader and libc, which `/usr` and the symlinks
    already cover, and its cache under the real HOME is deliberately left outside -- that
    directory can hold private index credentials, which is precisely the surface an empty
    root closes. The cost is a sandboxed `uv` with no persistent package cache.
    """
    if cfg.toolchain_binds:
        return tuple(to_posix(b) for b in cfg.toolchain_binds)
    found = shutil.which("uv")
    return (found,) if found else ()


def resolve_home(cfg: Config) -> str:
    """`cfg.sandbox_home` as an absolute POSIX path, with `~` expanded.

    The default is written with a tilde because that is how an operator reads it, and bwrap
    binds paths, not shell words -- a literal `~` names a directory called `~`.

    Deliberately `posixpath` and `$HOME` rather than `os.path.expanduser`, which follows the
    *host's* rules: on Windows it yields a drive-letter path with backslashes, which is not
    a thing bwrap can bind. Everything below the WSL boundary is POSIX-only, and a helper
    that quietly changes shape depending on where the test suite runs is worse than one that
    is explicit about it.
    """
    raw = to_posix(cfg.sandbox_home)
    if raw == "~" or raw.startswith("~/"):
        home = os.environ.get("HOME") or posixpath.expanduser("~")
        raw = home if raw == "~" else posixpath.join(home, raw[2:])
    return posixpath.normpath(raw)


def ensure_home(home: str) -> None:
    """Create the persistent sandbox HOME on the host, if it is not there yet.

    A bind needs its source to exist. bwrap will happily create the mount point *inside* the
    sandbox and then fail on the source with "Can't find source path", which reads as a
    mistyped setting rather than as a directory nobody has made yet -- so this runs before
    every call rather than once at startup, where a deleted cache directory would turn every
    later command into that same misleading error.
    """
    os.makedirs(home, exist_ok=True)


def available(cfg: Config) -> bool:
    """True if `cfg.bwrap_bin` resolves to something runnable.

    The one filesystem question `build_argv` refuses to ask, so that argv construction stays
    pure and assertable where no `bwrap` is installed.
    """
    return shutil.which(cfg.bwrap_bin) is not None


def _resolv_conf_target() -> str | None:
    """The real path behind `/etc/resolv.conf`, or None if there is nothing to bind.

    Under WSL that file is a symlink into `/mnt/wsl/`, so binding `/etc` binds a dangling
    link: connections by address succeed while connections by name fail (ADR-0021). Binding
    the target at its own path is what makes DNS work when the network is shared at all.
    """
    try:
        real = os.path.realpath("/etc/resolv.conf")
    except OSError:
        return None
    return real if os.path.exists(real) else None


# A name to probe a directory with. The denylist's directory entries are written as
# `.ssh/**`, which matches what is *inside* the directory and not the directory itself --
# so asking `secret_match` about `~/.ssh` returns nothing, and the whole directory would go
# unshadowed while every file under it matched. Asking about a child instead is what the
# pattern was written to answer. fnmatch has no globstar, so `**` is an ordinary `*` that
# also spans `/`, which is why one probe covers nested children too.
#
# The name must match nothing on its own: no dot, so `*.pem` and `*.key` cannot fire, and
# none of the substrings the list looks for. A pattern like `*credential*` that would match
# through the probe also matches the directory's own path, which the direct check catches.
_DIR_PROBE = "delegate-probe"


def _dir_match(path: str, globs: Sequence[str]) -> str | None:
    """A directory is a secret if it matches, or if the list says its contents are."""
    return secret_match(path, globs) or secret_match(f"{path}/{_DIR_PROBE}", globs)


def load_opaque_globs(cfg: Config) -> tuple[str, ...]:
    """The bulk-directory list, or an empty one. Never fatal. ADR-0041.

    Deliberately not `load_secret_globs`, and deliberately not the same file. That list is
    a security control and a missing one is fatal, because a denylist matching nothing
    reads exactly like a denylist that passed. This one only decides what the walk skips,
    so an absent file costs time and nothing else -- and refusing to start over it would
    turn a performance aid into an outage.

    Kept apart from the denylist for a second reason: sharing the file would let a slow
    scan be "fixed" by editing the security list, which is the one edit nobody should make
    for a performance reason.
    """
    raw = cfg.opaque_globs_file.strip()
    if not raw:
        return ()
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning(
            "no opaque-directory list at %s; every directory will be walked, which is "
            "slow on a tree carrying a virtualenv or node_modules",
            path,
        )
        return ()
    return tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _dir_shadow(
    path: str, globs: Sequence[str], opaque: Sequence[str]
) -> ShadowTarget | None:
    """The mount that should cover this directory, or None to walk into it.

    The denylist is asked first, so a directory on both lists is reported as the secret it
    is rather than as bulk -- the `matched` field is what an operator reads to tell whether
    the security list fired, and it must not claim credit for a speed measure.

    Both answers are the same mount. That is the point: an opaque directory is covered
    exactly as a matched secret one is, so skipping its contents costs no coverage. A
    secret inside it is hidden by the mount over its parent whether or not this walk ever
    looks. Pruning without covering is the hole, and it is the tempting shape. (ADR-0041)
    """
    glob = _dir_match(path, globs)
    if glob is not None:
        return ShadowTarget(path=path, kind="dir", matched=glob)
    bulk = _dir_match(path, opaque)
    if bulk is not None:
        return ShadowTarget(path=path, kind="dir", matched=f"opaque:{bulk}")
    return None


def discover_secret_shadows(cfg: Config, req: SandboxRequest) -> tuple[ShadowTarget, ...]:
    """Every denylist match under the bound roots, as a mount that will cover it up. I/O.

    An empty root means a denylist cannot work by subtraction: there is nothing to subtract
    from, and a secret is only ever visible because it sits inside a tree that had to be
    bound whole. So it is covered up afterwards instead, which needs a concrete path -- and
    the denylist holds patterns. Hence a walk.

    **`home`, `workdir` and `extra_binds` are all scanned.** The first two always were. The
    third was excluded, deliberately, on two grounds that were written down and have since
    stopped holding: that `extra_binds` are paths "an operator chose, typically somewhere
    under `/usr`", and that covering a file inside a read-only bind protects nothing the
    bind did not already protect.

    The first premise died when M6 let an agent *file* supply `extra_binds`. A markdown file
    that anyone can add to a repository is not an operator decision, and the argument for
    trusting the value was entirely that an operator had typed it. The second was never
    quite right for credentials specifically: read-only still means readable, and reading is
    the whole of the threat for a secret -- a read-only bind stops it being edited, which
    nobody was worried about.

    Recorded rather than deleted, because the reasoning outliving the code it justified is a
    failure this repository has now seen twice: the same thing happened to the comment
    explaining `WITHHELD_TOOL_NAMES` when that set was emptied. (ADR-0035)

    **Symlinks are skipped, and that is not laziness.** Emitting a shadow op on a symlink
    node does not follow it and does not create it -- it aborts the entire bwrap invocation
    with `Can't mount tmpfs on ...: No such file or directory`, measured against a running
    bwrap 0.9.0. Fail-closed, so nothing leaks, but a `~/.ssh` symlinked into a dotfiles
    repository is common enough that every `run_bash` call would die naming neither the
    denylist nor the link. Skipping loses less than it looks: a link's target is either
    inside a bound root, where this walk reaches it by its real path anyway, or outside
    every bound root, where the sandbox never bound it and the link simply dangles.

    What this leaves uncovered, stated rather than discovered later: a link whose *name*
    matches the denylist but whose *target* does not -- `.ssh` pointing at `realssh`. That
    is the same gap `paths.py` has, because it also resolves before matching, and the two
    share `secret_match` precisely so they cannot disagree about it. Both layers match real
    paths. Neither matches names that merely point at something.

    The scan is point-in-time. A file the command itself writes afterwards is not covered,
    and cannot be: `run_bash` holds a read-write bind for the whole call. This is
    defence-in-depth for one tool, not the authority `paths.py` is for `read_file`.
    """
    roots: list[str] = [req.home]
    if req.workdir is not None and req.workdir != req.home:
        roots.append(req.workdir)
    roots.extend(b for b in req.extra_binds if b not in roots)

    globs = load_secret_globs(cfg)
    opaque = load_opaque_globs(cfg)
    found: list[ShadowTarget] = []
    seen: set[str] = set()
    budget = cfg.secret_shadow_max_entries

    for root in roots:
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            if dirpath.rstrip("/").count("/") - base_depth >= cfg.secret_shadow_max_depth:
                raise SecretShadowIncomplete(
                    f"the secret scan reached {cfg.secret_shadow_max_depth} directories "
                    f"deep under {root} and stopped. run_bash is refused rather than run "
                    "with a denylist that covered only part of the tree. Raise "
                    "DELEGATE_SECRET_SHADOW_MAX_DEPTH if the tree is genuinely that deep."
                )

            budget -= len(dirnames) + len(filenames)
            if budget < 0:
                raise SecretShadowIncomplete(
                    f"the secret scan visited more than {cfg.secret_shadow_max_entries} "
                    f"entries under {root} and stopped. run_bash is refused rather than "
                    "run with a denylist that covered only part of the tree. Usually the "
                    "tree carries a machine-generated directory nobody needs to scan: name "
                    "it in the opaque list (DELEGATE_OPAQUE_GLOBS_FILE) and it is covered "
                    "and skipped, which is both faster and no less safe. Raising "
                    "DELEGATE_SECRET_SHADOW_MAX_ENTRIES makes every call walk it instead."
                )

            kept: list[str] = []
            for name in dirnames:
                full = posixpath.join(dirpath, name)
                if os.path.islink(full):
                    continue  # never descended, never shadowed; see the docstring
                shadow = _dir_shadow(full, globs, opaque)
                if shadow is None:
                    kept.append(name)
                    continue
                # Matched: shadow it and do not descend. Pruning is what keeps `.git/**`
                # from walking every loose object, and it also guarantees no shadow is ever
                # emitted inside another one, where the outer tmpfs would hide the target
                # the inner op needs to exist.
                if full not in seen:
                    seen.add(full)
                    found.append(shadow)
            dirnames[:] = kept

            for name in filenames:
                full = posixpath.join(dirpath, name)
                if os.path.islink(full):
                    continue
                glob = secret_match(full, globs)
                if glob is not None and full not in seen:
                    seen.add(full)
                    found.append(ShadowTarget(path=full, kind="file", matched=glob))

    return tuple(found)


def build_argv(
    cfg: Config, req: SandboxRequest, shadows: Sequence[ShadowTarget] = ()
) -> list[str]:
    """Config plus a request in, the exact bwrap argv out. Pure.

    **Bind order carries meaning.** bwrap applies binds in argv order and a later bind
    shadows an earlier one at or below the same path, so the sequence below is a set of
    rules rather than a stylistic choice:

    1. HOME binds before the workdir. A workdir nested inside `sandbox_home` then gets its
       own read-write bind rather than inheriting whatever mode HOME was bound with.
    2. Read-only toolchain binds come before the read-write workdir. If an operator's
       toolchain bind overlaps the workdir, the workdir must still be writable -- otherwise
       a build fails with a read-only-filesystem error inside the very directory the
       operator chose to run it in, which names the wrong cause.

    3. Secret shadows come after every bind, because a shadow covers up a path *inside* a
       tree that a bind created, and bwrap would have nothing to mount on if it ran first.
       `shadows` is a parameter rather than something computed here: finding them is a
       filesystem walk, and folding I/O into this function would make the two rules above
       assertable only on a machine with a real bwrap and a real tree -- which on Windows,
       where a contributor's first `pytest` happens, means not at all. `run` does the walk.

    Both rules are asserted directly in the tests, because both are invisible until the day
    the paths overlap.
    """
    argv: list[str] = [cfg.bwrap_bin, *_BASE_ARGV]

    # HOME first (rule 1). Note there is no `--dir` here: bwrap creates a bind's mount point
    # inside the sandbox by itself, but it cannot invent the *source*, and `--dir` makes a
    # directory in the sandbox rather than on the host. A missing host directory fails with
    # "Can't find source path", which reads like a bad configuration value rather than a
    # directory nobody has created yet. `run` creates it; see `ensure_home`.
    argv += ["--bind", req.home, req.home]

    # Read-only toolchain binds before the workdir (rule 2).
    for bind in req.extra_binds:
        argv += ["--ro-bind", bind, bind]

    if req.workdir is not None:
        # Read-write: a shell that cannot write its own working directory cannot run a
        # build or a test suite, which is most of why run_bash exists.
        argv += ["--bind", req.workdir, req.workdir]

    # Rule 3: after every bind, so each shadow has something to mount on.
    for shadow in shadows:
        if shadow.kind == "dir":
            argv += ["--tmpfs", shadow.path]
        else:
            # A tmpfs needs a directory. /dev/null is the file-shaped equivalent: readable
            # as a mount source, and an unreadable empty thing once it is in place.
            argv += ["--ro-bind", "/dev/null", shadow.path]

    if req.network:
        argv.append("--share-net")
        resolv = _resolv_conf_target()
        if resolv is not None:
            argv += ["--ro-bind", resolv, resolv]

    argv += ["--chdir", req.workdir if req.workdir is not None else req.home]

    argv.append("--clearenv")
    argv += ["--setenv", "HOME", req.home]
    argv += ["--setenv", "PATH", SANDBOX_PATH]
    for name in sorted(req.env):
        argv += ["--setenv", name, req.env[name]]

    argv += ["--", "/bin/sh", "-c", req.command]
    return argv


def resolve_env(cfg: Config) -> dict[str, str]:
    """The host environment names a sandboxed command may inherit, with their values.

    HOME and PATH are set explicitly by `build_argv` and are not sourced from the host, so
    they are excluded here even if an operator names them: inheriting the real HOME would
    undo the one property the empty root is for.
    """
    names = set(BUILTIN_ENV_ALLOWLIST) | set(cfg.env_passthrough)
    names -= {"HOME", "PATH"}
    return {n: os.environ[n] for n in sorted(names) if n in os.environ}


def run(cfg: Config, req: SandboxRequest) -> SandboxResult:
    """Run one command inside the sandbox, or refuse. The only function here with I/O.

    Refusal is the whole point of the `available` check: when bwrap is missing this raises,
    and there is no branch below it that runs the command anyway.

    The process gets its own session so that a timeout kills the whole group. `--die-with-
    parent` covers bwrap outliving *this server*, which is a different failure from a command
    hanging while the server is perfectly healthy; without the process group, killing bwrap
    on timeout can leave its grandchildren running.
    """
    if not available(cfg):
        raise SandboxUnavailable(
            f"{cfg.bwrap_bin!r} was not found, so run_bash cannot be confined. This server "
            "does not run shell commands unconfined as a fallback (ADR-0010). Install "
            "bubblewrap, or set DELEGATE_BWRAP_BIN to its path."
        )

    ensure_home(req.home)
    argv = build_argv(cfg, req, discover_secret_shadows(cfg, req))
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=cfg.run_bash_timeout,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        log.warning("sandboxed command exceeded %ss and was killed", cfg.run_bash_timeout)
        return SandboxResult(
            stdout=_as_text(e.stdout),
            stderr=_as_text(e.stderr),
            exit_code=None,
            timed_out=True,
        )
    return SandboxResult(
        stdout=proc.stdout,
        stderr=proc.stderr,
        exit_code=proc.returncode,
        timed_out=False,
    )


def _as_text(raw: str | bytes | None) -> str:
    """Whatever the process managed to emit before it was killed.

    `TimeoutExpired` carries bytes even when the call asked for text, so this is not the
    redundant branch it looks like.
    """
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
