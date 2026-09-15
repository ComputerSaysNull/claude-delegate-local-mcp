"""`run()` dropped git's output whenever it contained a character cp1252 cannot decode.

`subprocess.run(..., text=True)` with no `encoding` decodes with the ambient codec, which is
cp1252 on this machine. `❌` is `E2 9D 8C`, and byte `0x9D` is undefined there. The decode
happens on subprocess's own reader thread, so the `UnicodeDecodeError` kills that thread and
never reaches the caller: `run()` gets back `returncode` **0** and `stdout` **None**, then
dies on `.strip()` with an `AttributeError` pointing nowhere near the cause.

It surfaced while verifying a pull request whose commit message had cancelled a roadmap item
and therefore carried `❌`. The gate crashed in `--mode ci` and the wrapper around it reported
nothing, so a local pre-publish check silently did no checking at all.

**CI is why this survived.** The workflow runs on Linux, where the ambient codec is UTF-8 and
the decode always succeeds, so the gate that guards every pull request could not fail this way
in the only place it runs automatically. The red below is therefore Windows-only: on Linux
this file passes with or without the fix, which is worth knowing rather than hiding, since it
is the same asymmetry that let the bug exist.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

# Assembled rather than written inline so this file's own bytes are not the thing under test.
CANCELLED = "❌"


def load_gate():
    """The gate is a script, not a package, so it is loaded by path.

    It must be registered in `sys.modules` before executing: its `@dataclass` resolves
    annotations through `sys.modules[cls.__module__]`, and an unregistered module makes that
    lookup return None — a failure that looks nothing like the one under test.
    """
    spec = importlib.util.spec_from_file_location("_gate_decoding", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_gate_decoding"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, capture_output=True)
    (r / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    msg = r / "msg.txt"
    msg.write_text(f"fix: an item marked {CANCELLED} cancelled\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-q", "--no-verify", "-F", str(msg)],
                   cwd=r, capture_output=True)
    return r


def test_run_returns_output_containing_a_character_the_ambient_codec_lacks(repo, monkeypatch):
    gate = load_gate()
    monkeypatch.setattr(gate, "ROOT", repo)
    out = gate.run("git", "log", "--format=%B", "HEAD")
    assert isinstance(out, str), "stdout came back None: the reader thread died decoding"
    assert CANCELLED in out, f"the character was lost in decoding: {out!r}"


def test_run_still_returns_plain_ascii_output(repo, monkeypatch):
    """The other direction, so a `run` that returned a constant would not pass the first."""
    gate = load_gate()
    monkeypatch.setattr(gate, "ROOT", repo)
    assert gate.run("git", "rev-parse", "HEAD").strip(), "a plain call stopped working"


def test_run_reports_a_failing_command_as_empty_rather_than_raising(repo, monkeypatch):
    """A command that fails has no stdout, and that must stay a string."""
    gate = load_gate()
    monkeypatch.setattr(gate, "ROOT", repo)
    assert gate.run("git", "rev-parse", "--verify", "refs/heads/nope") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="the ambient codec is only cp1252 here")
def test_the_ambient_codec_really_cannot_decode_it():
    """Pins the premise. If Windows ever defaults to UTF-8, the red above stops being a red
    and this says so rather than letting the file quietly become decorative."""
    with pytest.raises(UnicodeDecodeError):
        CANCELLED.encode("utf-8").decode("cp1252")
