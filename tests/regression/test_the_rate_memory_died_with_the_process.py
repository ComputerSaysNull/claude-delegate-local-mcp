"""Four of six passes died at zero turns because reconnecting the MCP emptied the memory.

`RateHistory` outlives every delegation in a server process and nothing else. Reconnecting
the client starts a new process, so the memory resets to empty, `expect` finds nothing at
any concurrency, and every delegation falls through to `seed_decode_rate`'s other source:
the cluster's since-boot mean, blended over every concurrency the engine has ever served.

Measured 2026-09-12. That blend read **34.96 tok/s** where six concurrent delivers just
under 20 -- 1.75x optimistic. The 37,756 tokens it authorised need 1,888s of an 1,800s
turn, and two of four admitted passes died there having completed no turn. The memory that
would have priced them correctly had been collected seconds earlier, by the same server,
for the same cluster.

The fix is not a warm-up: a synthetic sample is a guess at a concurrency and an effort no
real work met, entering a structure whose `min()` makes one bad sample permanent. It is to
keep what was actually measured.

**Where it goes, and why that is not the blocked state-directory question.**
`slots.default_dir()` is tmpfs -- `XDG_RUNTIME_DIR`, else `/dev/shm` -- and the roadmap
recorded that as wrong for this. Measured 2026-09-13: tmpfs already survives a reconnect,
which is the whole failure, and the slots directory there outlived the distro boot. A rate
*should* die when the machine is rebuilt, because the hardware it describes may have
changed; a `served_model_id` stamp covers a model swap in between. So this needs nothing
the transcript's durability decision is waiting on.

Persistence is worth having because the floor shared in ADR-0073 made good samples rarer,
not because it made them absent: measured across every transcript on this host, **599 of
1144 turns clear 512 tokens**, so slightly over half of all turns still teach the memory
and a reconnect was throwing all of them away.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_delegate_local.loop import RateHistory

# Above the shared floor, so they are real observations rather than refused ones.
SIX_WAY = (2301, 118.6)     # 19.4 tok/s, the benchmarked rate at six concurrent
SOLO = (2272, 51.5)         # 44.1 tok/s, the benchmarked rate alone
STAMP = "a-served-model"


def test_a_second_process_reads_what_the_first_measured(tmp_path: Path):
    """The bug. A reconnect is a new process, and the memory did not outlive one."""
    first = RateHistory(path=tmp_path / "rates.json", stamp=STAMP)
    first.observe(*SIX_WAY, concurrency=6)

    second = RateHistory(path=tmp_path / "rates.json", stamp=STAMP)
    assert second.expect(6) is not None, "a reconnect threw away a real measurement"
    assert second.expect(6) == first.expect(6)


def test_the_worst_sample_survives_rather_than_the_last(tmp_path: Path):
    """`expect` takes a minimum, so persistence has to carry every sample, not a summary."""
    first = RateHistory(path=tmp_path / "rates.json", stamp=STAMP)
    first.observe(*SOLO, concurrency=1)
    first.observe(*SIX_WAY, concurrency=6)

    second = RateHistory(path=tmp_path / "rates.json", stamp=STAMP)
    # The six-way sample answers the quieter question too: contention only slows a stream.
    assert second.expect(1) == second.expect(6)


def test_a_model_swap_discards_the_memory(tmp_path: Path):
    """The rate belongs to a model as much as to the hardware, and the model moves.

    Without this the memory is worse than useless after a swap: it is confidently wrong,
    and `min()` keeps the wrongest sample for 64 observations.
    """
    first = RateHistory(path=tmp_path / "rates.json", stamp=STAMP)
    first.observe(*SIX_WAY, concurrency=6)

    swapped = RateHistory(path=tmp_path / "rates.json", stamp="a-different-model")
    assert swapped.expect(6) is None


def test_a_damaged_file_is_not_an_outage(tmp_path: Path):
    """Started empty, never raised. The memory is an optimisation, not a dependency."""
    broken = tmp_path / "rates.json"
    broken.write_text('{"version": 1, "seen": [[6, 19.4', encoding="utf-8")

    recovered = RateHistory(path=broken, stamp=STAMP)
    assert recovered.expect(6) is None
    recovered.observe(*SIX_WAY, concurrency=6)
    assert recovered.expect(6) is not None


def test_a_foreign_payload_is_not_trusted_into_the_deque(tmp_path: Path):
    """Shape, not just parseability. A file on tmpfs is writable by this user's processes."""
    hostile = tmp_path / "rates.json"
    hostile.write_text(
        json.dumps({"version": 1, "stamp": STAMP,
                    "seen": [["six", "fast"], [6], [], [6, 19.4]]}),
        encoding="utf-8",
    )
    assert RateHistory(path=hostile, stamp=STAMP).expect(6) == 19.4


def test_an_unbacked_memory_still_works(tmp_path: Path):
    """The control. Every existing caller builds one with no path and must be unaffected."""
    h = RateHistory()
    h.observe(*SIX_WAY, concurrency=6)
    assert h.expect(6) is not None
    assert not list(tmp_path.iterdir())


def test_a_refused_sample_is_not_written(tmp_path: Path):
    """The floor is upstream of persistence, so a poisoned sample cannot be made durable."""
    h = RateHistory(path=tmp_path / "rates.json", stamp=STAMP)
    h.observe(105, 1.002, concurrency=2)   # 104.8 tok/s, the ADR-0073 shape
    assert RateHistory(path=tmp_path / "rates.json", stamp=STAMP).expect(2) is None
