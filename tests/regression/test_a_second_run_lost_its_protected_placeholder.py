"""A `run_bash` failed to start because another run removed a placeholder it had bound.

A missing protected directory is created before the walk, bound read-only, and removed
after the command if still empty. A second run in the same workdir that starts while the
placeholder exists does not own it, so when the first run's settle removes it between the
second run's walk and its bwrap start, bwrap refuses: `Can't find source path .../.idea`.
Seen when two delegations ran beside each other in one workdir. Nothing had run, so the
fix prepares again and starts once more.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from pathlib import Path

from test_a_sandboxed_command_changed_a_file_the_host_acts_on import _run, needs_sandbox

from claude_delegate_local import sandbox


def _other_run_settles_first(monkeypatch, work: Path) -> list[list[str]]:
    """Remove `.idea` just before the first start, as the other run's settle would."""
    real = sandbox._execute
    starts: list[list[str]] = []

    def execute(cfg, argv, marker):
        if not starts:
            (work / ".idea").rmdir()
        starts.append(argv)
        return real(cfg, argv, marker)

    monkeypatch.setattr(sandbox, "_execute", execute)
    return starts


@needs_sandbox
def test_a_placeholder_removed_by_another_run_does_not_fail_this_one(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / ".idea").mkdir(parents=True)  # the other run's placeholder, not this run's
    starts = _other_run_settles_first(monkeypatch, work)

    result = _run(work, "echo ok")

    assert result.exit_code == 0, result
    assert result.stdout.strip() == "ok"
    assert len(starts) == 2
    assert not (work / ".idea").exists(), "the retry's own placeholder outlived the command"


@needs_sandbox
def test_a_command_that_fails_is_not_started_twice(tmp_path, monkeypatch):
    """The negative control: only bwrap refusing a vanished protected path is retried."""
    work = tmp_path / "work"
    work.mkdir()
    real = sandbox._execute
    starts: list[list[str]] = []

    def execute(cfg, argv, marker):
        starts.append(argv)
        return real(cfg, argv, marker)

    monkeypatch.setattr(sandbox, "_execute", execute)

    result = _run(
        work,
        f'echo "bwrap: Can\'t find source path {work}/gone: No such file or directory" >&2; '
        "exit 1",
    )

    assert result.exit_code == 1, result
    assert len(starts) == 1, "a command that ran and failed was started again"
