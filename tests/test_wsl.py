r"""The Windows-to-POSIX boundary.

Every test here is about a translation that would otherwise fail *quietly*: a path that
comes out plausible-looking and resolves to nothing, so the refusal the caller eventually
sees names the workspace-root layer for what was really a boundary bug.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.wsl import UntranslatablePath, is_windows_form, to_posix


@pytest.mark.parametrize(
    "given,expected",
    [
        (r"C:\Users\me\proj\src\foo.py", "/mnt/c/Users/me/proj/src/foo.py"),
        ("C:/Users/me/proj/src/foo.py", "/mnt/c/Users/me/proj/src/foo.py"),
        # Claude Code has no reason to be consistent about separators within one call.
        (r"C:/Users/me\proj/src\foo.py", "/mnt/c/Users/me/proj/src/foo.py"),
        (r"D:\data\x.json", "/mnt/d/data/x.json"),
        # Lowercase drive letters exist and must not produce /mnt/C.
        (r"c:\x.py", "/mnt/c/x.py"),
        ("C:\\", "/mnt/c/"),
        ("C:", "/mnt/c/"),
    ],
)
def test_a_drive_letter_becomes_a_mount_point(given, expected):
    assert to_posix(given) == expected


@pytest.mark.parametrize(
    "given",
    [
        "/mnt/c/Users/me/proj/src/foo.py",
        "/home/dev/x.py",
        "/tmp/a b/c.py",
    ],
)
def test_a_posix_path_is_passed_through_untouched(given):
    assert to_posix(given) == given


def test_a_backslash_in_a_posix_path_is_not_a_separator():
    """The reason translation is conditional rather than unconditional.

    A backslash is a legal character in a POSIX filename. Rewriting separators on every
    path would corrupt the ones that were already correct, and the corruption would show
    up as a missing file rather than as a translation error.
    """
    assert to_posix(r"/home/dev/odd\name.py") == r"/home/dev/odd\name.py"


@pytest.mark.parametrize(
    "given,expected",
    [
        (r"\\wsl$\Ubuntu-24.04\home\dev\x.py", "/home/dev/x.py"),
        (r"\\wsl.localhost\Ubuntu-24.04\home\dev\x.py", "/home/dev/x.py"),
        # Case-insensitive: Explorer and PowerShell disagree about capitalisation.
        (r"\\WSL.LOCALHOST\Ubuntu-24.04\home\dev\x.py", "/home/dev/x.py"),
    ],
)
def test_a_wsl_unc_path_translates_by_deletion(given, expected):
    """It is a Windows path naming a file that already lives here, so nothing is added."""
    assert to_posix(given) == expected


def test_a_network_share_is_refused_rather_than_invented():
    """The failure this prevents is the quiet one.

    Passing a UNC share through produces a path with no mount point behind it. It would
    then be refused by layer 1 for being outside every root -- a true statement about the
    wrong problem, sending the caller to edit their workspace roots.
    """
    with pytest.raises(UntranslatablePath) as e:
        to_posix(r"\\fileserver\share\x.py")
    assert "network" in str(e.value).lower()
    assert "mount point" in str(e.value)


def test_a_forward_slash_unc_is_refused_too():
    """`//server/share` is the same path in the other separator style."""
    with pytest.raises(UntranslatablePath):
        to_posix("//fileserver/share/x.py")


def test_an_empty_path_is_refused():
    with pytest.raises(UntranslatablePath):
        to_posix("   ")


def test_surrounding_whitespace_is_stripped():
    """A path pasted by a model routinely carries a newline from whatever produced it.

    Without this the refusal names a path that prints identically to the correct one,
    which is close to the least actionable message the policy can produce.
    """
    assert to_posix("  C:\\x.py\n") == "/mnt/c/x.py"


@pytest.mark.parametrize(
    "given,windows",
    [
        (r"C:\x.py", True),
        ("C:/x.py", True),
        (r"\\wsl$\Ubuntu\x", True),
        (r"\\server\share", True),
        ("/mnt/c/x.py", False),
        ("src/x.py", False),
    ],
)
def test_is_windows_form_agrees_with_what_to_posix_does(given, windows):
    assert is_windows_form(given) is windows
