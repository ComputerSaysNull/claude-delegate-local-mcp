"""`false | tail -1` exits 0, so a pipeline whose earlier stage failed reads as a clean run.

`run_bash` reports the whole line's status, so a failing stage before a successful last
stage leaves no trace and the delegation counts the call as clean. `pipeline_hid_a_failure`
reads the recorded `stages` and marks such a call as a masked failure instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

from claude_delegate_local import sandbox


# Only the end-to-end tests need a POSIX host; the pure rule runs everywhere, Windows too.
_POSIX = Path("/bin/sh").exists()


@lru_cache(maxsize=1)
def _sandbox_unusable() -> str:
    """Empty when a real sandbox actually runs here, else why it does not.

    `sandbox.available` asks whether the binary exists, which is a different question:
    a GitHub runner has `bwrap` installed and cannot create the user namespace it needs.
    Probed by running one trivial command, because that is the only thing that answers it.
    """
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",))  # type: ignore[arg-type]
    if not sandbox.available(cfg):
        return "bwrap is not installed"
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        try:
            probe = sandbox.run(cfg, sandbox.SandboxRequest(command="true", home=home))
        # Broad on purpose: any failure at all here means "cannot run one".
        except Exception as e:
            return f"bwrap could not run: {e}"
    if probe.exit_code != 0:
        return (
            "THE END-TO-END SANDBOX TESTS DID NOT RUN -- bwrap is installed but cannot "
            f"run a command here (`true` exited {probe.exit_code}: "
            f"{probe.stderr.strip()[:200]}). Unprivileged user namespaces are usually "
            "the cause. The masked-failure stage rule is unproven by this run."
        )
    return ""


needs_sandbox = pytest.mark.skipif(
    not _POSIX or bool(_sandbox_unusable()),
    reason=_sandbox_unusable() if _POSIX else "POSIX shell required; the sandbox never runs on Windows",
)


# ---- the pure rule ------------------------------------------------------------------


def test_a_failing_earlier_stage_is_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("pytest -q", 1), ("tail -40", 0)),)
    ) is True


def test_a_missing_file_is_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("cat missing", 1), ("head -1", 0)),)
    ) is True


def test_a_grep_no_match_is_not_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("grep absent f", 1), ("head -1", 0)),)
    ) is False


def test_a_sigpipe_into_a_reader_is_not_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("yes", 141), ("head -1", 0)),)
    ) is False


def test_a_diff_difference_is_not_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("diff a b", 1), ("head", 0)),)
    ) is False


def test_a_diff_error_is_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("diff a b", 2), ("head", 0)),)
    ) is True


def test_an_unrecognised_status_is_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("git status --short", 128), ("head -30", 0)),)
    ) is True


def test_a_name_value_prefix_is_stripped() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("LC_ALL=C grep x f", 1), ("head", 0)),)
    ) is False


def test_an_absolute_path_names_the_command() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("/usr/bin/grep x f", 1), ("head", 0)),)
    ) is False


def test_a_failing_stage_before_a_benign_one_is_still_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("false", 1), ("grep x", 1), ("head", 0)),)
    ) is True


def test_a_nonzero_last_stage_is_not_this_functions_case() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("echo hi", 0), ("grep absent", 1)),)
    ) is False


def test_a_sigpipe_with_no_later_zero_is_a_masked_failure() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("yes", 141), ("head -1", 1)),)
    ) is True


def test_an_unparseable_command_is_not_benign() -> None:
    assert sandbox.pipeline_hid_a_failure(
        ((("echo 'unbalanced", 2), ("head", 0)),)
    ) is True


# ---- end to end, against a real sandbox ----------------------------------------------


@needs_sandbox
def test_a_failed_pipeline_stage_is_seen_through_a_real_sandbox(tmp_path: Path) -> None:
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="false | tail -1", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.exit_code == 0, "the line still exits 0 -- that is the bug being reported"
    assert result.masked_failure is True


@needs_sandbox
def test_a_sigpipe_pipeline_is_not_a_masked_failure_through_a_real_sandbox(tmp_path: Path) -> None:
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="yes | head -1", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.masked_failure is False


@needs_sandbox
def test_a_grep_no_match_is_not_a_masked_failure_through_a_real_sandbox(tmp_path: Path) -> None:
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    target = tmp_path / "f"
    target.write_text("hello\n", encoding="utf-8")
    req = sandbox.SandboxRequest(command=f"grep absent {target} | head -1", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.masked_failure is False
