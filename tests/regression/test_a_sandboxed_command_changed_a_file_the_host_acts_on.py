"""`run_bash` could change files a host program acts on (PLAN M13.7, the sandbox half; R3).

With a `workdir`, a sandboxed command holds a read-write bind of the project. The mount-level
denylist hides secrets; nothing stopped a command rewriting `.claude/settings.json` or
`CLAUDE.md`, which take effect on the host before anyone reads a diff. The write tools
refuse those paths since #297, and that is no cover here: `paths.py` and `sandbox.py` are
independent layers, and `run_bash` never goes through the first.

The fix binds each existing protected path read-only onto itself, before the secret shadows
so a secret inside `.claude/` stays hidden. A missing one is the hard case, measured: bwrap
cannot bind over nothing, and binding anyway leaves a placeholder on the host. So a missing
protected directory is created, bound, and removed again if still empty, and a protected
file the command creates at the workdir root is moved aside and reported. The design is
docs/specs/2026-09-23-host-acted-paths.md.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from claude_delegate_local import config as config_module
from claude_delegate_local import sandbox


def _cfg():
    return config_module.Config(workspace_roots=(".",))  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def _sandbox_unusable() -> str:
    """Empty when a real sandbox runs here, else why not. As the masked-failure test says:
    an installed bwrap that cannot unshare is the host, not the feature, being broken."""
    if not Path("/bin/sh").exists():
        return "POSIX only: the sandbox never runs on Windows"
    cfg = _cfg()
    if not sandbox.available(cfg):
        return "bwrap is not installed"
    with tempfile.TemporaryDirectory() as home:
        try:
            probe = sandbox.run(cfg, sandbox.SandboxRequest(command="true", home=home))
        except Exception as e:  # any failure here means "cannot run one"
            return f"bwrap could not run: {e}"
    if probe.exit_code != 0:
        return (
            "THE END-TO-END SANDBOX TESTS DID NOT RUN -- bwrap cannot run a command here "
            f"(`true` exited {probe.exit_code}). The binds are unproven by this run."
        )
    return ""


needs_sandbox = pytest.mark.skipif(bool(_sandbox_unusable()), reason=_sandbox_unusable())


# ---- pure: the argv, wherever the suite runs ---------------------------------------------


def test_protected_paths_bind_read_only_before_any_secret_cover():
    """Rule 3a. The read-only bind of `.claude` must land before the cover of a secret
    inside it, or the bind would mount the real file back over its own cover."""
    req = sandbox.SandboxRequest(command="true", home="/h", workdir="/w")
    shadows = (
        sandbox.ShadowTarget(path="/w/.claude", kind="keep", matched=".claude/**"),
        sandbox.ShadowTarget(path="/w/.claude/.env", kind="file", matched=".env"),
    )
    argv = sandbox.build_argv(_cfg(), req, shadows)
    workdir = argv.index("--bind", argv.index("/h") + 1)
    keep = argv.index("/w/.claude")
    cover = argv.index("/w/.claude/.env")
    assert argv[keep - 1 : keep + 2] == ["--ro-bind", "/w/.claude", "/w/.claude"]
    assert workdir < keep < cover, argv


def test_only_literal_names_can_be_prepared():
    """A wildcard names nothing to create or to check for, so it is left to the walk."""
    dirs, files = sandbox._protected_names(
        (".claude/**", "CLAUDE.md", "!CLAUDE.md", "*.code-workspace", "a/b/**", ".mcp.json")
    )
    assert dirs == [".claude"]
    assert files == ["CLAUDE.md", ".mcp.json"]


# ---- end to end: a real bwrap ------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (work / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    return work


def _run(work: Path, command: str) -> sandbox.SandboxResult:
    home = work.parent / "home"
    return sandbox.run(
        _cfg(), sandbox.SandboxRequest(command=command, home=str(home), workdir=str(work))
    )


@needs_sandbox
@pytest.mark.parametrize("rel", [".claude/settings.json", "CLAUDE.md"])
def test_an_existing_protected_file_cannot_be_changed(project, rel):
    before = (project / rel).read_bytes()
    result = _run(project, f"echo planted > {rel}")
    assert result.exit_code != 0, result
    assert (project / rel).read_bytes() == before


@needs_sandbox
def test_nothing_new_can_be_put_inside_an_existing_protected_directory(project):
    result = _run(project, "echo planted > .claude/settings.local.json")
    assert result.exit_code != 0, result
    assert not (project / ".claude" / "settings.local.json").exists()


@needs_sandbox
def test_a_missing_protected_directory_cannot_be_created(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = _run(work, "mkdir -p .claude && echo planted > .claude/settings.json")
    assert result.exit_code != 0, result
    assert not (work / ".claude" / "settings.json").exists()
    assert not (work / ".claude").exists(), "the placeholder outlived the command"


@needs_sandbox
def test_a_protected_file_created_at_the_root_is_moved_aside(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = _run(work, "echo planted > CLAUDE.md")
    assert not (work / "CLAUDE.md").exists(), "a created CLAUDE.md was left in effect"
    assert (work / "CLAUDE.md.delegate-refused").read_text(encoding="utf-8") == "planted\n"
    assert result.protected_moved == (str(work / "CLAUDE.md"),)


@needs_sandbox
def test_the_rest_of_the_workdir_is_untouched(project):
    """The controls: ordinary files are writable, protected ones readable, and a secret
    inside a protected directory still covered."""
    (project / ".claude" / ".env").write_text("TOKEN=hidden-value\n", encoding="utf-8")
    result = _run(project, "echo ok > notes.md && cat CLAUDE.md; cat .claude/.env")
    assert (project / "notes.md").read_text(encoding="utf-8") == "ok\n"
    assert "# rules" in result.stdout
    assert "hidden-value" not in result.stdout
    assert result.protected_moved == ()
