"""`false; echo done` exited 0, so a delegation that hid a failure reported none.

`run_bash` ran the model's line under `/bin/sh` -- dash here -- and recorded the status of
the whole line. A command failing before the last one left no trace: `bash_failures` counted
it as a clean call and `last_bash_exit` read 0. ADR-0095 prepends an `ERR` trap under bash,
which fires on a *sequence* member as well as inside a pipeline's stages, and does not abort
the line.

Scope, measured rather than assumed (2026-09-19, in a real bwrap sandbox): the trap covers
`false; echo done` and does **not** cover `false | true`, because without `pipefail` the
pipeline's own status is the last stage's and no `ERR` occurs. `pipefail` was refused --
measured, it marks `grep <absent> | head` and `yes | head -1` as failures, which are
ordinary commands. The counter may therefore undercount and may never overcount, and
`test_a_pipeline_is_not_covered_and_that_is_deliberate` pins exactly that.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest
from conftest import BASELINE_MISSING, baseline_blob

from claude_delegate_local import sandbox
from claude_delegate_local.backends.base import BashOutcome, ToolResultBlock, ToolUseBlock
from claude_delegate_local.loop import _Watch


pytestmark = pytest.mark.skipif(
    not Path("/bin/sh").exists(), reason="POSIX shell required; the sandbox never runs on Windows"
)


@lru_cache(maxsize=1)
def _sandbox_unusable() -> str:
    """Empty when a real sandbox actually runs here, else why it does not.

    `sandbox.available` asks whether the binary exists, which is a different question:
    a GitHub runner has `bwrap` installed and cannot create the user namespace it needs,
    so every command comes back exit 1 with the real cause on stderr. Asserting against
    that reports the feature broken when it is the host that is.

    Probed by running one trivial command, because that is the only thing that answers
    it. The rest of the suite never noticed -- `build_argv` is pure precisely so the bind
    rules can be asserted where there is no working bwrap, and these are the first tests
    to execute one.
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
            "the cause. The trap itself is unproven by this run."
        )
    return ""


needs_sandbox = pytest.mark.skipif(bool(_sandbox_unusable()), reason=_sandbox_unusable())


def _call(n: int) -> ToolUseBlock:
    return ToolUseBlock(id=f"call-{n}", name="run_bash", input={"command": "x"})


def _result(n: int, outcome: BashOutcome) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=f"call-{n}", content="", bash=outcome)


# ---- the accounting, which is where the new fact has to survive -------------------


def test_an_early_masked_failure_survives_a_later_success() -> None:
    """The delegation-level claim: call 1 hid a failure, call 2 was clean.

    `bash_failures` accumulates, so it must still be non-zero at the end;
    `last_bash_exit` is overwritten by every call that ran, so it must read the *last*
    call's 0. Both at once is the misreport the item is about.
    """
    watch = _Watch(diagnostics=False)

    watch.called(
        _call(1), "ok",
        _result(1, BashOutcome(exit_code=0, ran=True, masked_failure=True)),
    )
    watch.called(
        _call(2), "ok",
        _result(2, BashOutcome(exit_code=0, ran=True, masked_failure=False)),
    )

    assert watch.bash_calls == 2
    assert watch.bash_masked_failures == 1
    assert watch.bash_failures == 1, "the masked failure must reach bash_failures"
    assert watch.last_bash_exit == 0, (
        "last_bash_exit still means the LAST call; ADR-0007 keeps it ambiguous on zero"
    )


def test_an_ordinary_non_zero_is_not_also_counted_as_masked() -> None:
    """A failure the status already reports is not hidden, and must not double-count."""
    watch = _Watch(diagnostics=False)

    watch.called(
        _call(1), "ok",
        _result(1, BashOutcome(exit_code=1, ran=True, masked_failure=False)),
    )

    assert watch.bash_failures == 1
    assert watch.bash_masked_failures == 0


def test_a_clean_call_counts_nothing() -> None:
    """The control. Without this the two assertions above pass on a counter stuck high."""
    watch = _Watch(diagnostics=False)

    watch.called(
        _call(1), "ok",
        _result(1, BashOutcome(exit_code=0, ran=True, masked_failure=False)),
    )

    assert watch.bash_failures == 0
    assert watch.bash_masked_failures == 0


# ---- the argv, which decides whether any of the above can ever be true -------------


def test_the_trap_is_prepended_and_the_shell_changes() -> None:
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="false; echo done", home="/tmp/home")

    argv = sandbox.build_argv(cfg, req, status_file="/tmp/home/.marker")

    assert argv[-3] == sandbox.MASKED_STATUS_SHELL
    assert argv[-2] == "-c"
    assert argv[-1].endswith("false; echo done")
    assert argv[-1].startswith("trap ")
    # The path travels as an environment variable, so the trap body carries no path at all
    # and a quote in `home` cannot rewrite it.
    assert "/tmp/home/.marker" not in argv[-1]
    assert sandbox.STATUS_FILE_ENV in argv


def test_without_the_status_shell_the_argv_is_exactly_what_it_was() -> None:
    """The fallback must be the old behaviour, not a degraded new one."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="false; echo done", home="/tmp/home")

    argv = sandbox.build_argv(cfg, req, status_file=None)

    assert argv[-3:] == [sandbox.PLAIN_SHELL, "-c", "false; echo done"]
    assert sandbox.STATUS_FILE_ENV not in argv


# ---- end to end, against a real sandbox --------------------------------------------


@needs_sandbox
def test_a_masked_failure_is_seen_through_a_real_sandbox(tmp_path: Path) -> None:
    """The whole path: dash's misreport is what `run` now contradicts."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="false; echo done", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.exit_code == 0, "the line still exits 0 -- that is the bug being reported"
    assert result.masked_failure is True
    assert "done" in result.stdout
    # The marker is cleaned up, or the next call inherits this one's verdict.
    assert not list(tmp_path.glob(".delegate-bash-status-*"))


@needs_sandbox
def test_a_wholly_successful_line_reports_nothing_masked(tmp_path: Path) -> None:
    """Without this the test above passes against a flag that is always true."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="true; echo fine", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.exit_code == 0
    assert result.masked_failure is False


@needs_sandbox
def test_a_handled_failure_is_not_a_masked_one(tmp_path: Path) -> None:
    """`if`, `||` and `!` test a status deliberately. Trapping those would be noise."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(
        command="if false; then echo no; fi; false || echo handled; ! false", home=str(tmp_path)
    )

    result = sandbox.run(cfg, req)

    assert result.masked_failure is False, "a tested failure is not a hidden one"


@needs_sandbox
def test_the_trap_does_not_change_what_the_line_means(tmp_path: Path) -> None:
    """The objection the item raised against `pipefail`: semantics must be untouched."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]

    assert sandbox.run(cfg, sandbox.SandboxRequest("exit 42", str(tmp_path))).exit_code == 42
    assert sandbox.run(cfg, sandbox.SandboxRequest("false", str(tmp_path))).exit_code == 1

    kept = sandbox.run(cfg, sandbox.SandboxRequest("echo out; echo err >&2", str(tmp_path)))
    assert kept.stdout.strip() == "out"
    assert kept.stderr.strip() == "err"


@needs_sandbox
def test_a_pipeline_is_not_covered_and_that_is_deliberate(tmp_path: Path) -> None:
    """Pins the scope, so nobody later reads the feature as covering more than it does.

    `pipefail` would cover this and was refused: measured, it marks `grep <absent> | head`
    and `yes | head -1` as failures. If someone adds it, this test fails and they have to
    argue with the reason rather than discover it.
    """
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="false | true; echo done", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.masked_failure is False, (
        "the pipeline half is out of scope; see ADR-0095 before changing this"
    )


# ---- the negative control ----------------------------------------------------------


def test_negative_control_the_committed_code_could_not_see_it() -> None:
    """Against the baseline: no trap, no field, and the plain shell hard-coded."""
    def show(path: str) -> str:
        blob = baseline_blob(path)
        if blob is None:
            pytest.skip(BASELINE_MISSING)
        return blob

    sandbox_blob = show("src/claude_delegate_local/sandbox.py")
    assert '"--", "/bin/sh", "-c", req.command' in sandbox_blob, (
        "the baseline no longer hard-codes the plain shell; this control cannot fail"
    )
    assert "masked_failure" not in sandbox_blob
    assert "ERR" not in sandbox_blob

    assert "masked_failure" not in show("src/claude_delegate_local/backends/base.py")

    loop_blob = show("src/claude_delegate_local/loop.py")
    assert "bash_masked_failures" not in loop_blob
    # The exact line that could not see it: three terms, none of them about a hidden one.
    assert (
        "self.bash_failures += is_error or bash.timed_out or bash.exit_code != 0"
        in loop_blob
    )

    assert "bash_masked_failures" not in show("src/claude_delegate_local/server.py")
