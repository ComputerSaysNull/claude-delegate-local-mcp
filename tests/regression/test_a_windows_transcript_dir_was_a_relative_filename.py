r"""`transcript_dir` skipped translation, so a Windows path became a relative filename.

Every other path setting -- `workspace_roots`, `effective_workdir_roots`, `sandbox_home`,
`toolchain_binds` -- is translated before it is trusted. `transcript_dir` was not. The
symptom PLAN.md recorded is the mild one: the operator converted the path by hand.

The real one is worse, and it is what these tests pin. A Windows path is a legal
*single-component* POSIX filename, so `C:\Users\me\t` is relative, not absolute, and not an
error. So `open_stream` called `mkdir(parents=True)` on it and **succeeded**, writing
records into a directory of that literal name under the server's working directory, and its
`except OSError` never fired because nothing raised. `check_transcripts` did the same
`mkdir` and returned `OK, "writable at C:\Users\me\t"` -- so `--doctor`, which its own
remedy called "the only place it is reported", **passed on the exact misconfiguration it
exists to catch**, and the advice it carried sat in a `FAIL` branch that failure could never
reach. That is the fifth check found here that could not fail.

**The fault is POSIX-only, and so are the tests that need a real path.** On Windows
`C:\Users\me\t` is genuinely absolute and nothing is wrong, which is why `transcript.py`
opens through `wsl.to_local` rather than `wsl.to_posix`: translating a location on the host
that wrote it invents a directory under the current drive. An earlier draft of this file
translated unconditionally, and the full Windows suite caught it -- 23 failures across
`test_transcript*.py`, every one a record written to `C:\mnt\c\...` instead of the
directory the caller named. The string-level tests below are host-independent; the ones
that resolve a location carry `POSIX_ONLY`.
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath

import pytest

from claude_delegate_local import doctor, transcript
from claude_delegate_local.config import Config
from claude_delegate_local.wsl import UntranslatablePath, to_local, to_posix

WINDOWS_FORM = r"C:\Users\me\transcripts"
POSIX_FORM = "/mnt/c/Users/me/transcripts"
UNC_FORM = r"\\fileserver\share\transcripts"

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="the fault needs a host where a Windows path is not already absolute",
)


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


# --- the bug itself, reconstructed. Host-independent: it is pure string handling. --------


def test_the_setting_form_was_one_relative_component_not_a_path():
    """What the old code computed, shown to be the bug rather than asserted about.

    Without this half the file only proves the fix works, not that the fault was what the
    CHANGELOG says it was. If this ever stops being a single relative component, the entry
    describing it is wrong.
    """
    was = PurePosixPath(os.path.expanduser(WINDOWS_FORM.strip()))
    assert was.parts == (WINDOWS_FORM,), "the fault is that this is ONE component"
    assert not was.is_absolute(), "and relative, so mkdir(parents=True) succeeds under cwd"


def test_translation_turns_it_into_a_real_absolute_path():
    translated = PurePosixPath(to_posix(WINDOWS_FORM))
    assert translated.is_absolute()
    assert translated.parts[:3] == ("/", "mnt", "c")
    assert str(translated) == POSIX_FORM


def test_to_local_translates_only_where_the_boundary_is_real():
    """The distinction the 23-failure Windows run forced, pinned on both hosts."""
    if os.name == "posix":
        assert to_local(WINDOWS_FORM) == POSIX_FORM
    else:
        assert to_local(WINDOWS_FORM) == WINDOWS_FORM, "already absolute here"
    assert to_local(POSIX_FORM) == POSIX_FORM, "a POSIX path is never rewritten"


def test_surrounding_whitespace_is_stripped_on_either_host():
    """`to_local` strips on both branches, so dropping the old `.strip()` changed nothing."""
    assert to_local(f"  {POSIX_FORM}\n") == POSIX_FORM


# --- resolving an actual location. POSIX only, because that is where the fault lives. ----


@POSIX_ONLY
def test_the_directory_is_translated_rather_than_taken_literally():
    resolved = PurePosixPath(transcript.directory(cfg(transcript_dir=WINDOWS_FORM)).as_posix())
    assert resolved.is_absolute(), "the whole fault was that this was False"
    assert str(resolved) == POSIX_FORM


@POSIX_ONLY
def test_a_posix_transcript_dir_is_untouched():
    """The idempotence `to_posix` claims, tested rather than trusted."""
    resolved = transcript.directory(cfg(transcript_dir=POSIX_FORM)).as_posix()
    assert resolved == POSIX_FORM


@POSIX_ONLY
def test_a_unc_transcript_dir_is_refused_rather_than_invented():
    """There is no mount point for a share here, so a plausible-looking path is worse."""
    with pytest.raises(UntranslatablePath):
        transcript.directory(cfg(transcript_dir=UNC_FORM))


@POSIX_ONLY
def test_open_stream_swallows_an_untranslatable_directory():
    """This module may not raise into a delegation -- the module docstring's own rule.

    `UntranslatablePath` is a `ValueError`, not an `OSError`, so the original
    `except OSError` would have let it escape into `run_delegation` once translation was
    added. `write` already caught broadly; `open_stream` did not.
    """
    assert transcript.open_stream(cfg(transcript_dir=UNC_FORM), "agent") is None


# --- the doctor. Host-independent: it is the coupling that is under test, not the path. --


def test_the_doctor_translates_through_transcript_rather_than_by_hand(tmp_path, monkeypatch):
    """The check that could not fail now asks where records will actually land.

    Pinned by substitution rather than by running the real `mkdir`. Asserting on a real
    translated path means asserting `/mnt/c/...` exists, and an earlier draft of this test
    quietly created `C:\\mnt\\c\\...` on the Windows host to make itself pass -- the same
    mistake as the check it was written for. What matters is that `doctor` stopped doing
    its own path handling.
    """
    seen: dict[str, str] = {}

    def spy(c: Config):
        seen["raw"] = c.transcript_dir
        return tmp_path

    monkeypatch.setattr(doctor.transcript, "directory", spy)
    check = doctor.check_transcripts(cfg(transcript_dir=WINDOWS_FORM))

    assert seen["raw"] == WINDOWS_FORM, "doctor must hand the raw setting to transcript"
    assert check.verdict == doctor.OK
    assert WINDOWS_FORM not in check.detail, "reporting the raw setting is the old bug"
    assert str(tmp_path) in check.detail


def test_the_doctor_fails_a_unc_directory_with_a_message_not_a_traceback():
    """FAIL on either host, though for different reasons: `UntranslatablePath` on POSIX,
    and a real `OSError` from an unreachable share on Windows. Both must carry a remedy."""
    check = doctor.check_transcripts(cfg(transcript_dir=UNC_FORM))
    assert check.verdict == doctor.FAIL
    assert check.remedy
