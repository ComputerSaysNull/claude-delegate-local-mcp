"""The decode-rate memory lived on tmpfs, so a reboot priced off the since-boot mean.

`server.build` and `run_task` both derived the path from `slots.default_dir_if_available`,
which is `$XDG_RUNTIME_DIR/claude-delegate-local` or `/dev/shm/...` -- tmpfs in both
branches. No setting named it, so an operator could not move it either. ADR-0094 makes it
durable and gives it `rate_history_dir`.

The negative control loads the committed module by path and asserts the new behaviour is
*absent* from it, which is how this repository proves a red when the fix and its test land
together.
"""

from __future__ import annotations


import importlib.util
import sys
from pathlib import Path

import pytest
from conftest import BASELINE_MISSING, baseline_blob

from claude_delegate_local import config as config_module
from claude_delegate_local.loop import RateHistory
from claude_delegate_local.slots import rate_history_path


def _cfg(**overrides: object) -> config_module.Config:
    # `workspace_roots` is required and has no default, so it is supplied rather than
    # meaningful here -- nothing in this file resolves a path through the policy.
    return config_module.Config(workspace_roots=(".",), **overrides)  # type: ignore[arg-type]


def test_a_configured_directory_is_where_the_memory_lands(tmp_path: Path) -> None:
    """The setting names the directory, and the filename is joined for the caller."""
    target = tmp_path / "durable"
    path = rate_history_path(_cfg(rate_history_dir=str(target)))

    assert path == target / "rate-history.json"


def test_samples_written_by_one_process_come_back_to_the_next(tmp_path: Path) -> None:
    """The point of the whole change: re-instantiating recovers what was seen.

    Two `RateHistory` objects over one path stand in for two runs of the server. Nothing
    here is mocked -- the first writes through the real `_write`, the second reads through
    the real `_read`.
    """
    path = rate_history_path(_cfg(rate_history_dir=str(tmp_path)))
    assert path is not None

    # Both samples clear MIN_TOKENS (512) and MIN_SECONDS (1.0), or `observe` drops them
    # and this would pass for the wrong reason.
    first = RateHistory(path=path, stamp="a-model")
    first.observe(2350, 100.0, concurrency=4)   # 23.5 tok/s
    first.observe(1925, 100.0, concurrency=6)   # 19.25 tok/s
    assert path.exists(), "observe did not persist; the rest of this test is vacuous"

    second = RateHistory(path=path, stamp="a-model")

    # `trusted` reads the bucket itself; untrusted widens to every busier one, so the
    # four-way question would be answered by the six-way sample and prove less.
    assert second.expect(4, trusted=True) == pytest.approx(23.5)
    assert second.expect(6, trusted=True) == pytest.approx(19.25)
    # And the widening still works across the reload, which is the property `expect` has.
    assert second.expect(4) == pytest.approx(19.25)


def test_a_blank_setting_keeps_the_old_runtime_directory(tmp_path: Path) -> None:
    """Blank is the previous behaviour rather than an error, and never a crash.

    `default_dir` reads `os.getuid`, so the guard matters on Windows, where this suite
    runs and the server does not. Either a runtime path or None is correct; an
    `AttributeError` is what this pins against.
    """
    path = rate_history_path(_cfg(rate_history_dir=""))

    assert path is None or path.name == "rate-history.json"


def test_the_default_is_not_tmpfs() -> None:
    """A default under the runtime directory would reinstate the bug silently."""
    default = _cfg().rate_history_dir

    assert default
    assert "/dev/shm" not in default
    assert "XDG_RUNTIME_DIR" not in default


def test_negative_control_the_committed_code_had_no_such_setting() -> None:
    """Against the baseline, the setting does not exist and the path is not configurable.

    Loaded by path from git rather than by import, so the working tree is untouched and
    no stash is involved. `sys.pycache_prefix` is not in play because nothing is imported
    from the checkout -- the source is read from the object database into memory.
    """
    blob = baseline_blob("src/claude_delegate_local/config.py")
    if blob is None:
        pytest.skip(BASELINE_MISSING)

    assert "rate_history_dir" not in blob, (
        "the baseline commit already has the setting; this control can no longer fail"
    )

    slots_blob = baseline_blob("src/claude_delegate_local/slots.py")
    assert slots_blob is not None
    assert "def rate_history_path" not in slots_blob
    assert "def default_dir_if_available" in slots_blob

    server_blob = baseline_blob("src/claude_delegate_local/server.py")
    assert server_blob is not None
    # The bug itself: the path came from the tmpfs runtime directory, joined inline.
    assert 'rate_dir / "rate-history.json"' in server_blob
    assert "rate_dir = default_dir_if_available()" in server_blob


def test_negative_control_the_committed_helper_lands_under_the_runtime_dir(
    tmp_path: Path,
) -> None:
    """Drive the baseline's own helper and show where it puts the file.

    This is the measurement the docstring above promises: not that the old code lacks a
    name, but that it resolves to tmpfs whatever the operator sets.
    """
    blob = baseline_blob("src/claude_delegate_local/slots.py")
    if blob is None:
        pytest.skip(BASELINE_MISSING)
    source = tmp_path / "old_slots.py"
    source.write_text(blob, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("old_slots", source)
    assert spec is not None and spec.loader is not None
    old = importlib.util.module_from_spec(spec)
    sys.modules["old_slots"] = old
    try:
        spec.loader.exec_module(old)
        runtime = old.default_dir_if_available()
    finally:
        del sys.modules["old_slots"]

    if runtime is None:  # no fcntl -- the memory was per-process, which is the other half
        pytest.skip("no POSIX runtime directory on this platform")

    text = str(runtime)
    assert "/dev/shm" in text or "run" in text, text
