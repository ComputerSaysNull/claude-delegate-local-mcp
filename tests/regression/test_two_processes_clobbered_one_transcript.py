"""Two processes could name one dispatch identically, and the record write truncates.

`_COUNTER` is a module global, so every process starts at 0001. A transcript filename was
a timestamp to the millisecond, that counter and the agent slug -- no pid, no uuid, no
random suffix -- so two processes dispatching to the same agent inside one millisecond
built the same name. The stream is opened append-only and would interleave; the record is
opened `O_TRUNC`, so the second process silently overwrote the first's.

Not hypothetical: the `run` subcommand makes cross-process fan-out ordinary, and the
transcript directory is shared by every process that writes one.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC

from test_server import cfg

from claude_delegate_local import slots, transcript


class FrozenClock:
    """One millisecond, for as long as the test wants it.

    The bug is only reachable inside a single millisecond, so a test that lets the clock
    move cannot see it -- and would pass against the unfixed code for the wrong reason.
    """

    FIXED = datetime(2026, 9, 21, 12, 0, 0, 123_000, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED


def _as_a_fresh_process(monkeypatch, token: str) -> None:
    """Put the module in the state a *second* process would be in, in the same tick.

    The clock is frozen and the counter reset because that is the whole point: a new
    process starts at zero, so the number cannot be what tells two of them apart. The
    token is set with `raising=False` so this reads as "the discriminator differs"
    against the fixed module and changes nothing against the unfixed one -- where the
    finding is then the two files becoming one, not an attribute that is missing.
    """
    monkeypatch.setattr(transcript, "datetime", FrozenClock)
    monkeypatch.setattr(transcript, "_COUNTER", {"n": 0})
    monkeypatch.setattr(transcript, "_PROCESS", token, raising=False)


def test_two_processes_do_not_overwrite_each_others_record(tmp_path, monkeypatch) -> None:
    """The harm itself: the record is opened O_TRUNC, so one of the two is simply gone.

    Asserted on the directory rather than on a filename, because the directory is what
    an operator has afterwards and one file where there should be two is the whole bug.
    """
    config = cfg(transcript_dir=str(tmp_path))

    for token, task in (("aaaaaa", "the first process"), ("bbbbbb", "the second")):
        _as_a_fresh_process(monkeypatch, token)
        transcript.write(
            config, agent_name="docs-audit-local", entry=None, task=task, workdir=None,
            prefetched=None, lease=None, dispatched=None, error=None, started=0.0,
        )

    written = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(written) == 2, (
        f"two dispatches, {len(written)} record(s): {written}. Same millisecond, same "
        "agent, and the same counter -- which starts at zero in every process -- so "
        "they built one name, and O_TRUNC made the second erase the first."
    )
    tasks = sorted(json.loads((tmp_path / n).read_text(encoding="utf-8"))["task"]
                   for n in written)
    assert tasks == ["the first process", "the second"], tasks


def test_two_processes_do_not_append_to_each_others_stream(tmp_path, monkeypatch) -> None:
    """The stream is opened append-only, so nothing is lost -- the two are braided.

    Which is worse to read than either is to lose: the events of two delegations in one
    file, in arrival order, with nothing in them saying which is which.
    """
    config = cfg(transcript_dir=str(tmp_path))

    for token in ("aaaaaa", "bbbbbb"):
        _as_a_fresh_process(monkeypatch, token)
        stream = transcript.open_stream(config, "docs-audit-local")
        assert stream is not None
        stream.start(tool="delegate", task=token, agent="docs-audit-local",
                     model_key="flash", effort="high")

    streams = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert len(streams) == 2, (
        f"two delegations, {len(streams)} stream(s): {streams}. Both appended to the "
        "same file, so a reader following it sees one run that did everything twice."
    )


def test_the_discriminator_is_the_process_identity_slots_already_uses(monkeypatch) -> None:
    """Not a second notion of "this process", and not a random suffix.

    A PIN, NOT A RED, and deliberately labelled one. The old code has no discriminator
    at all, so nothing can be asserted about where this one comes from except that the
    symbol is absent -- and an AttributeError is not evidence of anything we did not
    already know. The two tests above carry the red for this change; this one exists to
    stop the *next* edit replacing the derivation with something that merely looks right.

    It can still fail, which is what makes it worth keeping. A uuid per process would
    make the names unique and would make the transcript directory disagree with the slot
    file about who wrote what: it passes the first assertion and fails the second. A
    constant fails the first.
    """
    monkeypatch.setattr(slots, "_identity", lambda: "111:222")
    one = transcript._process_token()
    monkeypatch.setattr(slots, "_identity", lambda: "111:333")
    other = transcript._process_token()

    assert one != other, (
        "the same token for two different process identities: the token is not derived "
        "from the identity, so a reused pid would share a transcript name"
    )

    monkeypatch.setattr(slots, "_identity", lambda: "111:222")
    assert transcript._process_token() == one, (
        "the token moved without the identity moving. A random suffix would pass the "
        "assertion above and fail this one, and it is the one that says the transcript "
        "directory and the slot file are talking about the same process."
    )


def test_every_part_of_a_transcript_name_is_safe_in_a_filename(tmp_path) -> None:
    """`_slug` scrubs the agent name and nothing scrubs the rest, so the rest must arrive
    clean on its own.

    Asserted on a whole name that `open_stream` really produced, not on the new constant.
    That is what keeps it honest against both versions of the module: it passes on the
    old code, which is the truth -- the old name was safe, it was merely not unique --
    and it fails on the obvious wrong fix, which is to drop the process identity in
    verbatim. `slots._identity` joins two fields with a separator that is also a path
    separator on one of the two platforms this repository runs on.
    """
    config = cfg(transcript_dir=str(tmp_path))
    stream = transcript.open_stream(config, "docs-audit-local")
    assert stream is not None

    assert transcript._UNSAFE.search(stream.path.stem) is None, (
        f"{stream.path.name!r} carries a character a filename cannot. Every part of it "
        "but the agent slug is unscrubbed, so a raw identity dropped into one of them "
        "reaches the filesystem exactly as written."
    )


def test_a_stream_and_its_record_still_pair_up(tmp_path, monkeypatch) -> None:
    """A reader matches the two files of one dispatch by time, and must still be able to.

    The process token is inserted between the timestamp and the counter, so the stamp
    stays the leading field and the directory still sorts into time order. Deliberately
    asserts nothing about the counter: the stream is opened before the dispatch and the
    record written after it, so their numbers differ by however many dispatches ran in
    between, and pinning the number would be pinning the bug's own assumption.

    The clock is frozen so the two stamps are equal by construction rather than by luck.
    Left running, the pair straddles a millisecond boundary occasionally and this fails
    on a timer instead of on the thing it is about.
    """
    monkeypatch.setattr(transcript, "datetime", FrozenClock)
    config = cfg(transcript_dir=str(tmp_path))

    stream = transcript.open_stream(config, "docs-audit-local")
    assert stream is not None
    # A second dispatch between the two, so the counters cannot coincide by accident.
    transcript.open_stream(config, "someone-else")
    transcript.write(
        config, agent_name="docs-audit-local", entry=None, task="t", workdir=None,
        prefetched=None, lease=None, dispatched=None, error=None, started=0.0,
    )

    record = next(iter(tmp_path.glob("*.json")))
    assert transcript._PROCESS in stream.path.name, stream.path.name
    assert transcript._PROCESS in record.name, record.name
    # The stamp is still the first field, so the two sort together and a reader pairing
    # them by time is looking at the same place it always was.
    assert stream.path.name.split("-")[0] == record.name.split("-")[0], (
        f"{stream.path.name} and {record.name} no longer share a leading timestamp, so "
        "the only thing pairing a stream with its record is gone"
    )
    assert stream.path.name.endswith("-docs-audit-local.jsonl"), stream.path.name
    assert record.name.endswith("-docs-audit-local.json"), record.name
