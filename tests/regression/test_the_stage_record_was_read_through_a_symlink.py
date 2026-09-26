"""The stage record is read on the host from a path the sandboxed command can write to.

The marker sits on the read-write HOME bind and its path is in the command's environment,
so the command can replace it. Read with `read_bytes`, a symlink there made the host read
whatever it pointed at -- a host file outside every bind -- and any tab-separated lines in
it reached the operator transcript, and a link to `/dev/zero` was read whole before the
cap applied. The read must refuse anything that is not a regular file it opened itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import sandbox

pytestmark = pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt",
    reason="POSIX symlinks and FIFOs; the sandbox never runs on Windows",
)

RECORD = "0\tfalse\n0\ttail -5\n1 0\t\n"


def test_a_real_record_is_read(tmp_path: Path) -> None:
    """Control: a regular file holding a record is parsed, so the refusals below mean something."""
    marker = tmp_path / "marker"
    marker.write_text(RECORD)
    assert sandbox._read_stages(marker) == ((("false", 1), ("tail -5", 0)),)


def test_a_symlinked_marker_is_not_followed(tmp_path: Path) -> None:
    """The host must not read a file the command pointed the marker at."""
    target = tmp_path / "outside-every-bind"
    target.write_text(RECORD)
    marker = tmp_path / "marker"
    marker.symlink_to(target)
    assert sandbox._read_stages(marker) == ()


def test_a_fifo_marker_does_not_block(tmp_path: Path) -> None:
    """A named pipe at the marker path is not a record, and opening it must not wait."""
    marker = tmp_path / "marker"
    os.mkfifo(marker)
    assert sandbox._read_stages(marker) == ()
