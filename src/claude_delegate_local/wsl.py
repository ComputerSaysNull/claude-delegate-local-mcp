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

import re

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
