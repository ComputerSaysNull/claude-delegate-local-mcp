r"""The one place a Windows path becomes a POSIX one.

Claude Code runs on Windows; this server runs inside WSL2. `files[]` arrives as
`C:\Users\me\proj\src\foo.py`, while everything downstream -- `realpath`, the workspace
roots, `git check-ignore` -- is POSIX. Translation happens here and nowhere else, so a
wrong path has exactly one place to be wrong in. Everything below this is POSIX-only, per
CLAUDE.md.

Callers are not asked to send POSIX paths, because that pushes the translation onto the
model, where it fails quietly: a guessed `/c/Users/...` is a path that does not exist, and
the eventual refusal names the wrong layer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

# `C:\rest`, `C:/rest`, or a bare `C:` -- Claude Code emits all three, not necessarily
# consistently within one call.
_DRIVE = re.compile(r"^([A-Za-z]):([\\/].*)?$", re.DOTALL)

# `\\wsl$\Ubuntu-24.04\home\dev\x` and its `\\wsl.localhost\...` spelling: what Explorer
# hands out for a file already in the distribution. A Windows path naming a POSIX file, so
# it translates by deletion rather than by prefixing.
_WSL_UNC = re.compile(
    r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/]+[^\\/]+(?:[\\/]+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)


class UntranslatablePath(ValueError):
    """A path that cannot be expressed on this side of the boundary at all.

    Distinct from a path that translates cleanly and is then refused by policy: this one
    never reaches the policy, because there is nothing to check.
    """


def is_wsl() -> bool:
    """True when this POSIX process is running inside a WSL distribution.

    Here, not beside either caller, because it answers this module's question -- which
    side of the boundary this process is on. `--doctor` reports it and `--init` uses it to
    decide whether a registration goes through `wsl.exe`; a probe copied into both is how
    two answers start disagreeing.

    From procfs, not an environment variable: `WSL_DISTRO_NAME` is unset for a process
    systemd launches inside the distribution, which would report a WSL host as native.
    """
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def is_windows_form(given: str) -> bool:
    """True if `given` names a file the Windows way and must be translated."""
    s = given.strip()
    return bool(_DRIVE.match(s) or s.startswith("\\\\") or s.startswith("//"))


def to_posix(given: str) -> str:
    """Return `given` as a POSIX path, translating only if it is in Windows form.

    A path with no drive letter and no UNC prefix passes through untouched: it is already
    POSIX, and a backslash is legal in a POSIX filename, so rewriting separators
    unconditionally would corrupt correct paths.
    """
    # A model's path routinely carries a stray newline or trailing space. Windows names
    # cannot end in a space or a dot, and a POSIX one that does is far rarer than the paste
    # artefact, so stripping is right far more often -- and avoids a refusal naming a path
    # that looks, printed, exactly like the one meant.
    s = given.strip()
    if not s:
        raise UntranslatablePath("An empty string is not a path.")

    m = _WSL_UNC.match(s)
    if m:
        rest = (m.group(1) or "").replace("\\", "/")
        return "/" + rest

    if s.startswith("\\\\") or s.startswith("//"):
        # A network share has no mount point inside the distribution, and inventing one
        # would give a plausible path that resolves to nothing. Say so instead.
        raise UntranslatablePath(
            f"{given!r} is a UNC network path. The server runs inside WSL and cannot "
            "reach a Windows network share; copy the file into a workspace root, or "
            "mount the share and name it by its mount point."
        )

    m = _DRIVE.match(s)
    if m:
        drive, rest = m.group(1).lower(), (m.group(2) or "/").replace("\\", "/")
        return f"/mnt/{drive}{rest}"

    return s


def file_uri_to_posix(uri: str) -> str | None:
    """The POSIX folder a `file:` URI names, or None when it names none this side can reach.

    Here because an MCP client lists its roots as URIs, and on this topology they are
    Windows ones -- `file:///C:/Users/...` -- so this is the same boundary as `to_posix`,
    arriving in a different envelope. None rather than a raise: a root that names nothing
    reachable narrows nothing, and the caller decides whether what is left is enough.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file" or not parsed.path:
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"  # UNC: a `\\wsl$` spelling, or a network share
    elif re.match(r"^/[A-Za-z]:", path):
        path = path[1:]  # `/C:/Users` is the URI spelling of `C:/Users`
    try:
        posix = to_posix(path)
    except UntranslatablePath:
        return None
    # A drive-relative `C:foo` passes through untranslated, and `realpath` would resolve it
    # against this process's working directory: a folder the client never named.
    return posix if posix.startswith("/") else None


def to_local(given: str) -> str:
    """`to_posix` where a boundary was really crossed; a pass-through where it was not.

    `to_posix` translates unconditionally, which its comparing callers can afford: roots
    and the paths checked against them all go through it and then `realpath`, so on a
    Windows host the mangling applies to both sides of every comparison and cancels.

    A directory the server opens or writes into has no other side to cancel against, and
    translating it on a Windows host is a mistake: `C:\\Users\\me\\t` is already absolute
    there, and `/mnt/c/Users/me/t` names something under the current drive. The boundary
    exists only when this process is on its POSIX side -- always, in production -- so this
    differs from `to_posix` only under the Windows test suite.

    Use `to_posix` to compare a path. Use this to open one.
    """
    return to_posix(given) if os.name == "posix" else given.strip()


# `/mnt/c/rest`, the form `to_posix` makes from a drive letter. Only a single-letter mount
# matches: `/mnt/wsl` and `/mnt/some-share` are real directories with no Windows drive.
_MNT = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$", re.DOTALL)


def to_windows(given: str) -> str:
    r"""The inverse of `to_posix`, for a path that has to be *printed* to Windows.

    Here, not at its one call site, because a translation split across two modules is how
    the two directions stop agreeing. The server never uses it; `--init` does, printing an
    MCP registration whose `--cd` is read by `wsl.exe` on the Windows side, which takes
    the Windows form and rejects `/mnt/c/...` (README).

    Raises rather than guessing for a path with no Windows equivalent: `/home/you/x` is
    reached from Windows only by a `\\wsl$\...` spelling that needs the distribution
    name, which this is not given, and an invented one resolves to nothing.
    """
    s = given.strip()
    m = _MNT.match(s)
    if not m:
        raise UntranslatablePath(
            f"{given!r} has no Windows equivalent. Only a drive mount under /mnt "
            "translates back; a path inside the distribution is reachable from Windows "
            "only through a UNC spelling that needs the distribution's name."
        )
    drive, rest = m.group(1).upper(), (m.group(2) or "/").replace("/", "\\")
    return f"{drive}:{rest}"
