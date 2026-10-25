"""Wake state files outlived the processes that wrote them (ADR-0126).

The first live check of `wait-hook` found hundreds of handle files in the real runtime
directory, all from one WSL suite run: every test that built a server wrote there, and a
process that exits never removes its own. Two fixes, one test each: the suite gets a runtime
directory of its own, and a new `Handles` sweeps files whose server is gone.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_delegate_local import slots
from claude_delegate_local.handles import Handles


def _state(path: Path, pid: int) -> None:
    path.write_text(json.dumps({"handle": path.stem, "pid": pid, "status": "done"}))


def test_a_dead_servers_state_file_is_swept(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _state(tmp_path / "d-000000000dead.json", dead.pid)
    _state(tmp_path / "d-000000000live.json", os.getpid())

    Handles(60, state_dir=tmp_path)

    assert not (tmp_path / "d-000000000dead.json").exists()
    assert (tmp_path / "d-000000000live.json").exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="the runtime directory is POSIX-only")
def test_the_suite_never_writes_the_real_runtime_directory(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    assert slots.default_dir().resolve().is_relative_to(base), slots.default_dir()
