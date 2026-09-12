"""The gate crashed while printing a finding that was not pure ASCII.

The hooks run under Git Bash on Windows, where `sys.stdout.encoding` is cp1252. A finding
whose message carries a character outside that -- a roadmap marker quoted back, a smart
quote lifted from a document -- raised `UnicodeEncodeError` *inside the print*, so the
check fired and the gate died with a traceback instead of saying what it found.

Two details make it worse than a noisy crash. The printing loop runs over blocks **and
warnings**, and it runs before the verdict, so a non-ASCII *warning* killed a commit that
was about to pass. And the one environment that would have caught it is the one that hides
it: the agent's own shell exports PYTHONIOENCODING=utf-8, so the gate never crashed there
while crashing for a person running the same hook.

Every message written so far happens to be ASCII, which is why nothing had hit it. That is
a property of the messages, not of the gate, and the next message to quote a document is
the one that finds out.

The tests build their own cp1252 stream rather than leaning on the ambient one: under the
Windows suite stdout is UTF-8, so a test that trusted the environment would pass whether
the gate was fixed or not.

The first two assert the *premise* -- that cp1252 refuses the character, and that
`backslashreplace` keeps it legible -- and pass against the unfixed gate by design; they
exist so the premise starts failing loudly if it ever stops being true. The last two are
what guard the fix, and both fail against HEAD with `AttributeError` (verified 2026-09-12).

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import docs_gate  # noqa: E402  -- needs the path line above

TICK = "✅"  # the roadmap's done marker: the likeliest character to be quoted back


def cp1252_stream() -> io.TextIOWrapper:
    """What Git Bash hands the gate on Windows, built rather than assumed."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")


def test_a_non_ascii_finding_could_not_be_printed_at_all():
    """The bug itself, on an unreconfigured stream. The control is the ASCII case."""
    stream = cp1252_stream()

    ascii_finding = docs_gate.Finding(docs_gate.BLOCK, "roadmap-marker", "item is ticked")
    print("  " + str(ascii_finding), file=stream)  # the control: this must not raise

    marked = docs_gate.Finding(docs_gate.BLOCK, "roadmap-marker", f"item {TICK} is ticked")
    with pytest.raises(UnicodeEncodeError):
        print("  " + str(marked), file=stream)


def test_the_fix_makes_the_finding_printable_without_losing_it():
    """`backslashreplace` keeps the character visible as an escape rather than dropping it.

    Asserting the bytes rather than "it did not raise": a fix that swallowed the message,
    or replaced it with a question mark, would also not raise and would be worse than the
    crash, because a finding nobody can read is a finding nobody acts on.
    """
    stream = cp1252_stream()
    stream.reconfigure(errors="backslashreplace")

    marked = docs_gate.Finding(docs_gate.BLOCK, "roadmap-marker", f"item {TICK} is ticked")
    print("  " + str(marked), file=stream)
    stream.flush()

    written = stream.buffer.getvalue().decode("cp1252")
    assert "roadmap-marker" in written
    assert "\\u2705" in written, f"the character was lost rather than escaped: {written!r}"


def test_the_gate_reconfigures_both_streams_on_entry():
    """Both, because a finding is printed to stdout and a traceback lands on stderr.

    Called directly rather than through `main()`: the point is that the reconfiguration
    happens before any argument is parsed, so a crash in parsing still gets a readable
    message out.
    """
    original = sys.stdout, sys.stderr
    out, err = cp1252_stream(), cp1252_stream()
    sys.stdout, sys.stderr = out, err
    try:
        docs_gate._printable_findings()
        assert out.errors == "backslashreplace"
        assert err.errors == "backslashreplace"
    finally:
        sys.stdout, sys.stderr = original


def test_a_stream_that_cannot_be_reconfigured_is_not_fatal():
    """The helper must never become the cause of the crash it exists to prevent."""
    class Awkward:
        errors = "strict"

        def reconfigure(self, **_kw):
            raise ValueError("this stream does not take reconfiguration")

    original = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Awkward(), Awkward()
    try:
        docs_gate._printable_findings()  # must return, not raise
    finally:
        sys.stdout, sys.stderr = original
