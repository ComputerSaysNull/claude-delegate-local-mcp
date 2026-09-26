"""`last_bash_exit` is a whole shell line's status, so `false | tail -5` reads 0.

The `ERR` trap ADR-0095 added covers a *sequence* member but not a *pipeline*: a pipeline's
status is its last stage's, so a failing earlier stage leaves no trace and a delegation reads
as clean. The planned first move records, for the operator transcript only, every pipeline
(2+ stages) in which some stage exited non-zero -- each stage's command and status -- via a
`DEBUG` trap's `${PIPESTATUS[*]}`. These tests pin that record, the parser that reads it, and
the plumbing that carries it from `sandbox.run` through `tools.BashOutcome` and
`loop.ToolCallRecord` into `as_json()`. The feature does not exist yet, so every test here
must fail today.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

from claude_delegate_local import loop, sandbox, tools
from claude_delegate_local.backends.base import BashOutcome, ToolResultBlock, ToolUseBlock


pytestmark = pytest.mark.skipif(
    not Path("/bin/sh").exists(), reason="POSIX shell required; the sandbox never runs on Windows"
)


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
            "the cause. The stage record is unproven by this run."
        )
    return ""


needs_sandbox = pytest.mark.skipif(bool(_sandbox_unusable()), reason=_sandbox_unusable())


# ---- the parser, which reads the DEBUG-trap record file -------------------------------


def test_a_failing_pipeline_is_parsed_into_stages() -> None:
    """`false | tail -5; true` records one pipeline whose first stage failed.

    The `DEBUG` trap writes `<PIPESTATUS of the previous pipeline>\\t<next command>`, so the
    two-stage record sits on the line before the *next* command. Only that one pipeline is
    reported, and its stages pair the two commands appended before it with `1 0`.
    """
    text = "0\tfalse\n0\ttail -5\n1 0\ttrue\n0\t\n"
    assert sandbox.parse_stage_records(text) == ((("false", 1), ("tail -5", 0)),)


def test_a_clean_pipeline_is_not_recorded() -> None:
    """A pipeline whose every stage exited 0 is not a finding, and must not appear."""
    text = "0\ttrue\n0\tcat\n0 0\t\n"
    assert sandbox.parse_stage_records(text) == ()


def test_a_single_failing_command_is_not_a_pipeline() -> None:
    """`false; false` has no 2+ stage line, so it stays the ERR trap's case, not a pipeline."""
    text = "0\tfalse\n1\tfalse\n1\t\n"
    assert sandbox.parse_stage_records(text) == ()


# ---- the plumbing, which carries stages out to the transcript --------------------------


def test_pipeline_stages_flow_into_the_operator_transcript() -> None:
    """`tool_call_record` copies `result.bash.stages` into the record, and `as_json` emits it.

    The key is present as a list of lists of `{command, status}` objects -- the shape an
    operator reading the transcript can act on -- and only because stages are non-empty.
    """
    outcome = BashOutcome(
        exit_code=0, ran=True, masked_failure=False,
        stages=((("false", 1), ("tail -5", 0)),),
    )
    result = ToolResultBlock(tool_use_id="call-1", content="", bash=outcome)
    call = ToolUseBlock(id="call-1", name="run_bash", input={"command": "false | tail -5"})
    record = loop.tool_call_record(call, "ok", result)

    assert record.as_json()["stages"] == [
        [{"command": "false", "status": 1}, {"command": "tail -5", "status": 0}]
    ]


def test_no_stages_means_the_key_is_absent() -> None:
    """An empty `stages` is omitted, not reported as a measured empty.

    Following `_diagnostics_block`'s rule, a key present and empty reads as a measured empty
    -- which a call that ran no pipeline must not claim.
    """
    outcome = BashOutcome(exit_code=0, ran=True, masked_failure=False, stages=())
    result = ToolResultBlock(tool_use_id="call-1", content="", bash=outcome)
    call = ToolUseBlock(id="call-1", name="run_bash", input={"command": "true"})
    record = loop.tool_call_record(call, "ok", result)

    assert "stages" not in record.as_json()


# ---- end to end, against a real sandbox ------------------------------------------------


@needs_sandbox
def test_a_failing_pipeline_is_seen_through_a_real_sandbox(tmp_path: Path) -> None:
    """The whole path: a line that exits 0 still records the failing stage."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="false | tail -5", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.exit_code == 0, "the line still exits 0 -- that is the bug being reported"
    assert result.stages == ((("false", 1), ("tail -5", 0)),)


@needs_sandbox
def test_the_traps_leave_exit_status_and_pipestatus_as_they_were(tmp_path: Path) -> None:
    """The DEBUG trap runs before every command, so it must not change what `$?` reads."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    line = 'false; echo "$?"; false | true; echo "${PIPESTATUS[*]}"; (exit 42); echo "$?"'

    result = sandbox.run(cfg, sandbox.SandboxRequest(command=line, home=str(tmp_path)))

    assert result.stdout.split() == ["1", "1", "0", "42"]


@needs_sandbox
def test_a_clean_pipeline_reports_no_stages_through_a_real_sandbox(tmp_path: Path) -> None:
    """Without this the test above passes against a field that is always populated."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]
    req = sandbox.SandboxRequest(command="true | cat", home=str(tmp_path))

    result = sandbox.run(cfg, req)

    assert result.stages == ()


@needs_sandbox
def test_the_model_facing_text_does_not_mention_the_stage_record(tmp_path: Path) -> None:
    """The record is for the operator transcript; the model sees none of it."""
    from claude_delegate_local import config as config_module

    cfg = config_module.Config(workspace_roots=(".",), sandbox_home=str(tmp_path))  # type: ignore[arg-type]

    result = tools._run_bash(cfg, {"command": "false | tail -5"}, tools.BashPolicy())

    assert "stages" not in result.text
    assert sandbox.STAGES_FILE_ENV not in result.text
