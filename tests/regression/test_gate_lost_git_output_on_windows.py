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

import ast
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


def _is_true(node) -> bool:
    """An `ast` node spelling a literal `True`."""
    return isinstance(node, ast.Constant) and node.value is True


def text_mode_subprocess_calls_without_encoding(path: Path) -> list[int]:
    """Line numbers of text-mode subprocess calls in `path` that name no encoding.

    `subprocess.run(..., text=True)` (or `universal_newlines=True`) decodes with the
    ambient codec unless `encoding=` is given -- the very bug this file guards. Returns the
    offending line numbers rather than raising, so the test names them all at once and the
    negative control can call this directly on a doctored copy.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in (
            "run", "check_output", "Popen",
        ):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        if not (_is_true(kwargs.get("text")) or _is_true(kwargs.get("universal_newlines"))):
            continue
        if "encoding" not in kwargs:
            offenders.append(node.lineno)
    return offenders


def test_every_text_mode_git_call_in_the_gate_names_its_encoding():
    """`text=True` with no `encoding=` decodes with the ambient codec -- cp1252 on the
    Windows machine where this bug was found -- and loses characters that codec has no byte
    for. On Linux the default codec is already UTF-8, so today removing `encoding="utf-8"`
    from the gate's `subprocess.run` would make no test fail; this one is the check that
    does, and it names the line of every text-mode call the gate makes without one.
    """
    offenders = text_mode_subprocess_calls_without_encoding(
        ROOT / "scripts" / "docs_gate.py")
    assert not offenders, (
        "text-mode subprocess calls in scripts/docs_gate.py must name an encoding: "
        + ", ".join(f"line {n}" for n in offenders)
    )
