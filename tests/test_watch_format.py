"""The viewer reads the format version a stream names, and says when it cannot read it.

A reader that renders a stream in a format it does not know draws a confident picture of a
shape it is guessing at. A newer *minor* is fine by the versioning rules (ADR-0111): only
additions, which the viewer ignores. A different *major* gets said, once, at the head.
"""

from __future__ import annotations

from claude_delegate_local import transcript, watch

START = {"t": "start", "at": "2026-10-01T12:00:00+00:00", "tool": "delegate",
         "task": "x", "agent": None, "model_key": "flash", "effort": "high",
         "max_turns": 4, "tools": []}


def text(event: dict) -> str:
    return watch._plain("\n".join(watch.render(event, 100)))


def test_a_stream_in_another_major_format_says_so_at_its_head():
    assert "format 2.0" in text({**START, "format": "2.0"})


def test_a_stream_in_this_format_or_a_newer_minor_says_nothing_about_it():
    major = transcript.FORMAT.split(".")[0]
    for fmt in (transcript.FORMAT, f"{major}.99"):
        assert "format" not in text({**START, "format": fmt})


def test_a_stream_from_before_the_version_existed_says_nothing_about_it():
    """Absent is the shape every stream had until 1.0, and the viewer reads it as before."""
    assert "format" not in text(START)
