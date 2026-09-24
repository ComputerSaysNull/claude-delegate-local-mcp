"""A slot file that could not be locked in time failed the delegation, where it should degrade.

Every other shared-file call in `admission.py` falls back to this process's own counting on
`SlotsUnavailable`; the one that takes the slot did not, so a lock held past its timeout
failed a delegation the gate could have admitted. Falling back has a second half: a slot
taken only locally must not be handed back to the file, or it gives back one of this
process's other slots there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config
from claude_delegate_local.slots import SharedSlots, SlotsUnavailable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

posix_only = pytest.mark.skipif(
    fcntl is None or not Path("/proc").is_dir(),
    reason="the shared slot file needs fcntl and /proc",
)


def cfg() -> Config:
    return Config(  # type: ignore[arg-type]
        workspace_roots=(".",), max_inflight_seqs=4, admission_idle_hold=0.0,
        kv_token_budget=100_000,
    )


@posix_only
@pytest.mark.asyncio
async def test_an_unreachable_slot_file_admits_locally_and_leaves_the_file_alone(
    tmp_path, monkeypatch,
) -> None:
    slots = SharedSlots(tmp_path / "slots.json")
    slots.prepare()
    g = Admission(cfg(), slots)

    held = await g.acquire(100, entry_key="flash", entry_limit=4)   # counted in the file

    async def locked_out(**_kw):
        raise SlotsUnavailable("lock held past its timeout")

    real_admit = slots.admit
    monkeypatch.setattr(slots, "admit", locked_out)
    local = await g.acquire(100, entry_key="flash", entry_limit=4)
    monkeypatch.setattr(slots, "admit", real_admit)

    assert g.status()["inflight_seqs"] == 2
    await g.release(local)
    totals, _, _ = await slots.snapshot()
    assert totals.seqs == 1, "releasing the local-only slot gave back the file's other one"

    await g.release(held)
    totals, _, _ = await slots.snapshot()
    assert totals.seqs == 0
