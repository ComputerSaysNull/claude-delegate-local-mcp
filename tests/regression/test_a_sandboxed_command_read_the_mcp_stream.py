"""A sandboxed command read the server's stdin, which under stdio is the MCP stream.

`sandbox.run` passed no `stdin=`, so the shell inherited the server's fd 0. Under the
stdio transport that fd carries the client's JSON-RPC messages: a command that reads
stdin -- `cat` with no file, a REPL, a test that prompts -- consumes frames meant for the
server, so requests are lost and calls hang until a timeout. Measured on 2026-09-22: a
live delegation running `readlink /proc/self/fd/0` reported the server's own pipe.

`tools._run_git` already closed stdin. The other host-side subprocesses did not, so each
is pinned here, and the sandbox case is proven end to end with a real pipe on fd 0.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_delegate_local import doctor, paths, provision, sandbox
from claude_delegate_local.config import Config

MARKER = "jsonrpc-frame-meant-for-the-server"


def _sandbox_runs() -> bool:
    if not Path("/bin/sh").exists():
        return False
    cfg = Config(workspace_roots=(".",))  # type: ignore[arg-type]
    if not sandbox.available(cfg):
        return False
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        try:
            return sandbox.run(cfg, sandbox.SandboxRequest("true", home)).exit_code == 0
        except Exception:
            return False


@pytest.mark.skipif(not _sandbox_runs(), reason="needs a bwrap sandbox that can run a command")
def test_a_sandboxed_command_cannot_read_the_servers_stdin(tmp_path):
    read_end, write_end = os.pipe()
    os.write(write_end, (MARKER + "\n").encode())
    os.close(write_end)
    saved = os.dup(0)
    try:
        os.dup2(read_end, 0)
        result = sandbox.run(
            Config(workspace_roots=(str(tmp_path),)),  # type: ignore[arg-type]
            sandbox.SandboxRequest("cat", str(tmp_path)),
        )
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)
    assert MARKER not in result.stdout, "the sandboxed command read the server's stdin"


def _capture(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args[0] if args else kwargs.get("args"), 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _closed(kwargs: dict) -> bool:
    """Stdin is closed, or is data the caller supplied -- never the server's own fd 0."""
    return kwargs.get("stdin") is subprocess.DEVNULL or kwargs.get("input") is not None


def test_the_gitignore_layer_closes_stdin(monkeypatch):
    seen = _capture(monkeypatch)
    paths._git(["git", "--version"])
    assert seen and _closed(seen[0]), seen


def test_the_gitignore_layer_still_feeds_its_own_input(monkeypatch):
    seen = _capture(monkeypatch)
    paths._git(["git", "check-ignore", "--stdin"], stdin=b"a\0")
    assert seen[0].get("input") == b"a\0" and "stdin" not in seen[0], seen


def test_the_doctor_bwrap_probe_closes_stdin(monkeypatch):
    seen = _capture(monkeypatch)
    cfg = Config(workspace_roots=(".",))  # type: ignore[arg-type]
    monkeypatch.setattr(doctor.sandbox, "available", lambda _cfg: True)
    doctor.check_bwrap(cfg)
    assert seen and _closed(seen[0]), seen


def test_a_provision_build_step_closes_stdin(monkeypatch):
    seen = _capture(monkeypatch)
    provision._run(["true"], out=None)
    assert seen and _closed(seen[0]), seen
