r"""The one place a Windows path becomes a POSIX one.

Claude Code runs on Windows; this server runs inside WSL2. `files[]` therefore arrives as
`C:\Users\me\proj\src\foo.py`, while everything downstream of it -- `realpath`, the
workspace roots, `git check-ignore` -- is POSIX. Translation happens here and nowhere
else, so a path that came out wrong has exactly one place to be wrong in. Everything
below this line is POSIX-only, per CLAUDE.md.

The alternative was to require callers to send POSIX paths. That pushes the translation
onto the model, which is the kind of thing that fails quietly: a model that guesses
`/c/Users/...` produces a path that does not exist rather than an error saying so, and
the refusal it eventually gets names the wrong layer. Rejected.

Not a port. The ancestor project ran wholly on one operating system and had no boundary
to cross.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# `C:\rest`, `C:/rest`, or a bare `C:` -- Claude Code emits all three separator styles and
# has no reason to be consistent about them within one call.
_DRIVE = re.compile(r"^([A-Za-z]):([\\/].*)?$", re.DOTALL)

# `\\wsl$\Ubuntu-24.04\home\dev\x` and its newer `\\wsl.localhost\...` spelling: what
# Explorer hands out for a file that already lives in the distribution. It is a Windows
# path naming a POSIX file, so it translates by deletion rather than by prefixing.
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

    Lives here rather than beside either caller because it answers the question this
    module is about -- which side of the boundary this process is on. `--doctor` reports
    it in the platform row, and `--init` needs it to decide whether an MCP registration
    goes through `wsl.exe`; a four-line probe copied into both is how the two answers
    start disagreeing.

    Read from procfs rather than from an environment variable: `WSL_DISTRO_NAME` is unset
    for a process launched by systemd inside the distribution, which would report a WSL
    host as a native one.
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

    A path with no drive letter and no UNC prefix is passed through untouched -- it is
    already POSIX, and a backslash is a legal character in a POSIX filename, so rewriting
    separators unconditionally would corrupt paths that were correct.
    """
    # A path arriving from a model routinely carries a stray newline or trailing space
    # from whatever produced it. Windows filenames cannot end in a space or a dot, and a
    # POSIX one that does is vanishingly rarer than the paste artefact, so stripping is
    # the reading that is right far more often -- and the failure it avoids is a refusal
    # naming a path that looks, printed, exactly like the one the caller meant.
    s = given.strip()
    if not s:
        raise UntranslatablePath("An empty string is not a path.")

    m = _WSL_UNC.match(s)
    if m:
        rest = (m.group(1) or "").replace("\\", "/")
        return "/" + rest

    if s.startswith("\\\\") or s.startswith("//"):
        # A network share. There is no mount point for it inside the distribution, and
        # inventing one would produce a path that resolves to nothing while looking
        # plausible. Say so instead.
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


def to_local(given: str) -> str:
    """`to_posix` where a boundary was really crossed; a pass-through where it was not.

    `to_posix` translates unconditionally, and every caller it was written for can afford
    that because none of them uses the result as a *location*. `workspace_roots`,
    `effective_workdir_roots` and the requested paths checked against them all go through
    it and then through `realpath`, so on a Windows host the mangling applies to both sides
    of every comparison and cancels. Containment is answered correctly either way.

    A directory the server opens or writes into has no other side to cancel against. There,
    translating on a Windows host is not a no-op but a mistake: `C:\\Users\\me\\t` is
    already absolute there, and `/mnt/c/Users/me/t` names something under the current drive
    that nobody asked for. The boundary this module exists to cross only exists when this
    process is on the POSIX side of it -- which in production it always is, so this differs
    from `to_posix` only under the Windows test suite.

    Use `to_posix` to compare a path. Use this to open one.
    """
    return to_posix(given) if os.name == "posix" else given.strip()


# `/mnt/c/rest`, the form `to_posix` produces from a drive letter. Only a single-letter
# mount is matched: `/mnt/wsl` and `/mnt/some-share` are real directories inside the
# distribution that no Windows drive corresponds to.
_MNT = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$", re.DOTALL)


def to_windows(given: str) -> str:
    r"""The inverse of `to_posix`, for a path that has to be *printed* to Windows.

    Kept here rather than at its one call site, because a translation split across two
    modules is how the two directions stop agreeing. Nothing in the server uses it: the
    policy layers only ever translate toward POSIX, and a path this process opens is
    handled by `to_local`. What needs it is `--init`, which prints an MCP registration
    whose `--cd` is read by `wsl.exe` on the Windows side -- README says that argument
    takes the Windows form and that `/mnt/c/...` is rejected there, so printing the POSIX
    path would hand out a block that cannot work.

    Raises rather than guessing for a path with no Windows equivalent. `/home/you/x` lives
    inside the distribution and the `\\wsl$\...` spelling that reaches it needs the
    distribution name, which this function is not given -- and inventing one produces a
    path that looks plausible and resolves to nothing, the failure this module's own
    header rejects.
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
