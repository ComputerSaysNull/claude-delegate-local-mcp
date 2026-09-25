"""`provision <project>`: build the interpreter a delegation runs that project's tests with.

`run_bash` exists so a delegated model can verify its own work by running something, and
ADR-0007 rests the whole design on the server capturing that command's real exit code. For
a Python project none of it worked: inside the sandbox there is no `python` on `PATH`,
`import pytest` raises, and there is no network to install either. That was filed once as an
architectural limit (`#129`) and it was not one -- nothing had asked whether any *other*
path the sandbox binds could hold an interpreter. `sandbox_home` can: it is bound
read-write, it is persistent, and it sits outside the workspace, which ADR-0041 makes
mandatory rather than tidy.

**This runs server-side, which is where the network is.** A sandboxed command has no
network by design and that does not change (ADR-0063); provisioning is the step that needs
one, so it happens out here, once, as an operator action. `uv` is no substitute -- the
sandbox binds only its binary and leaves its cache outside deliberately, so inside an empty
root with no network it resolves nothing.

**Outside the workspace, and under `venvs/` rather than `.venv/`.** Both names are forced.
Inside a workspace root the secret scan walks the virtualenv and covers `certifi/cacert.pem`,
`keyring/credentials.py` and the whole of `secretstorage/` with `/dev/null`, breaking the
environment it just read (ADR-0041). And `security/opaque_globs.txt` names `.venv/**` and
`venv/**`, where a match is *covered* with a tmpfs -- so a venv called either would be
mounted over and invisible. `sandbox.PROVISIONED_DIRNAME` is the one tree the scan skips
without covering, and `build_argv` binds it read-only in exchange (ADR-0062).

**`posixpath`, never `os.path`.** Everything below the WSL boundary is POSIX-only
(`wsl.py` is the one place paths cross it), and the command refuses to run on Windows -- but
its *tests* run there, where a contributor's first `pytest` happens, and `os.path.join`
would hand them backslashes that no bind can use.

**The record beside each venv is what makes this discoverable.** `--doctor` finds
provisioned environments by walking the provisioned root and reading `provision.json`, so
there is no setting to keep in step with what was built, and no second place for the answer
to live.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, paths, sandbox
from .config import Config, ConfigError
from .paths import PathRefused
from .sandbox import provisioned_root, resolve_home

# The file recorded beside every provisioned environment. Read by `--doctor`, so the name is
# shared from here rather than written out twice.
RECORD_NAME = "provision.json"

# What the dependency hash is taken over: every file a build backend reads dependencies from.
# Deliberately whole files rather than the dependency tables inside them: the check is
# asymmetric, and hashing coarsely errs the safe way. A false "stale" costs one re-provision;
# a false "fresh" means a delegation tests against the wrong dependencies and returns the
# clean exit code ADR-0007 says to trust. `pyproject.toml` alone was a false fresh for any
# project declaring them in `setup.cfg` or `setup.py`.
HASH_SOURCES = ("pyproject.toml", "setup.cfg", "setup.py")

# Tool tables the build backend never reads, dropped before `pyproject.toml` is hashed, so
# rewording a pytest marker does not withhold a sound interpreter. `delegate-local` is ours:
# `nested-deselect` is read at call time and must not need a re-provision. A denylist on
# purpose: an unknown table still counts, so a mistake here costs one re-provision (a false
# "stale"), never the false "fresh" that hands a test run against the wrong dependencies a
# clean exit code.
NON_BUILD_TOOL_TABLES = ("pytest", "ruff", "mypy", "coverage", "pyright", "delegate-local")

# The environment name a sandboxed command finds the interpreter under. Absent when nothing
# current is provisioned for the workdir, never empty -- see `sandbox_env`.
SANDBOX_ENV_NAME = "DELEGATE_PYTHON"

# How long a build may take before it is abandoned. Generous: it is a virtualenv plus a
# dependency resolution over the network, and on a cold cache that is minutes.
_BUILD_TIMEOUT = 1800.0


class ProvisionError(RuntimeError):
    """Anything that stops a build, reported to the operator rather than raised at a caller."""


def venv_name(project_real: str) -> str:
    """A directory name for one project: its basename, plus a digest of its full path.

    The digest is not decoration. Two checkouts of the same repository -- a worktree, a
    second clone, a colleague's copy under a different parent -- have the same basename and
    different dependencies, and a name collision would silently hand one project the other's
    interpreter.
    """
    digest = hashlib.sha256(project_real.encode("utf-8")).hexdigest()[:8]
    return f"{posixpath.basename(project_real.rstrip('/')) or 'project'}-{digest}"


def venv_dir(home: str, project_real: str) -> str:
    """Where this project's environment lives, derived rather than configured."""
    return posixpath.join(provisioned_root(home), venv_name(project_real))


def interpreter_path(venv: str) -> str:
    """The absolute path a delegation invokes. Absolute, never a `PATH` entry.

    `sandbox.SANDBOX_PATH` stays `/usr/bin:/usr/sbin`: widening it would put one project's
    tools on every command's PATH, and the measurement that proved a provisioned venv works
    inside `bwrap --unshare-all` needed no PATH change at all.
    """
    return posixpath.join(venv, "bin", "python")


def _pyproject_bytes(raw: bytes) -> bytes:
    """The bytes `pyproject.toml` hashes to: a canonical form of what the build can read.

    Parsed with `tomllib`, the tool tables the build backend never reads dropped, and
    serialised with sorted keys so equivalent TOML hashes identically. Falling back to the
    raw bytes when it will not parse is the safe direction: a false "stale" costs one
    re-provision, a false "fresh" hands a test run against the wrong dependencies a clean
    exit code (ADR-0007).
    """
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return raw.replace(b"\r\n", b"\n")
    tool = data.get("tool")
    if isinstance(tool, dict):
        for name in NON_BUILD_TOOL_TABLES:
            tool.pop(name, None)
    return json.dumps(data, sort_keys=True, default=str).encode()


def dependency_hash(project_real: str) -> str | None:
    """A digest of the project's dependency declaration, or None if it has none.

    **Line endings are normalised first, and that is a bug fix rather than tidiness.**
    Measured 2026-09-08 on this repository, an hour after the digest shipped: the recorded
    hash was of the CRLF form, `git reset --hard` rewrote the file as LF, and identical
    content read as a changed declaration. `--doctor` then failed and `interpreter_for`
    withheld the interpreter, so `run_bash` lost the ability to run any test at all -- and
    nothing about the project had changed. On a Windows checkout that is not an edge case,
    it is every branch switch and every fresh clone.

    `pyproject.toml` is parsed and the tool tables the build never reads are dropped before
    hashing, so rewording a marker description under `[tool.pytest.ini_options]` does not
    make a sound environment look stale. The list is a denylist, and the asymmetry the
    original chose deliberately still holds: a false "stale" costs one re-provision, a false
    "fresh" hands back a clean exit code from a test run that proved nothing, so an unknown
    table still counts and a file that will not parse falls back to its raw bytes. The other
    sources stay whole-file, with newlines normalised -- a real edit still moves the digest,
    because a change to a dependency is a change to bytes that are not newlines.

    Every file in `HASH_SOURCES` that exists counts. One that exists and cannot be read makes
    the answer None -- "cannot tell" -- rather than leaving it out, which would be a false
    fresh. A project whose only declaration is `pyproject.toml` gets exactly the digest it
    always had, so an environment recorded before the other two counted is still current.
    """
    parts: list[tuple[str, bytes]] = []
    for name in HASH_SOURCES:
        try:
            raw = (Path(project_real) / name).read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if name == "pyproject.toml":
            raw = _pyproject_bytes(raw)
        else:
            raw = raw.replace(b"\r\n", b"\n")
        parts.append((name, raw))
    if not parts:
        return None
    if [name for name, _ in parts] == ["pyproject.toml"]:
        return hashlib.sha256(parts[0][1]).hexdigest()
    digest = hashlib.sha256()
    for name, raw in parts:
        # Name and length before the bytes, so no two different file sets can concatenate
        # to the same input.
        digest.update(f"{name}\0{len(raw)}\0".encode() + raw)
    return digest.hexdigest()


def _extras(project_real: str) -> tuple[str, ...]:
    """Which optional-dependency groups the project declares, so `dev` is not assumed."""
    source = Path(project_real) / "pyproject.toml"
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    optional = data.get("project", {}).get("optional-dependencies", {})
    return tuple(optional) if isinstance(optional, dict) else ()


def install_target(project_real: str) -> str:
    """The editable-install argument, carrying `[dev]` only when the project declares it."""
    return f"{project_real}[dev]" if "dev" in _extras(project_real) else project_real


def read_record(venv: str) -> dict[str, Any] | None:
    """The record beside a provisioned environment, or None if it is absent or unreadable."""
    try:
        raw = (Path(venv) / RECORD_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def discover(home: str) -> list[tuple[str, dict[str, Any] | None]]:
    """Every provisioned environment under the provisioned root, with its record.

    Returns a record of `None` for a directory that has one missing or unreadable, because
    that is a state `--doctor` has to report rather than skip: a half-built environment is
    exactly the case where a test run would pass against nothing.
    """
    root = provisioned_root(home)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [
        (posixpath.join(root, name), read_record(posixpath.join(root, name)))
        for name in names
        if os.path.isdir(posixpath.join(root, name))
    ]


def is_current(record: dict[str, Any] | None) -> bool:
    """Does this record describe an environment built from the project as it stands now?

    Both halves are required: the interpreter has to be there, and the declaration it was
    built from has to be unchanged. A missing digest on either side is *not* current --
    "cannot tell" and "matches" are different answers, and only one of them is safe.
    """
    if record is None:
        return False
    project = str(record.get("project") or "")
    python = str(record.get("interpreter") or "")
    if not project or not python or not os.path.exists(python):
        return False
    current = dependency_hash(project)
    return current is not None and current == record.get("dependency_hash")


def provisioned_for(cfg: Config, workdir: str | None) -> dict[str, Any] | None:
    """The record of the environment covering this workdir, or None. Never a stale one.

    A workdir *inside* a provisioned project counts, since an editable install works from
    anywhere under it, and the longest matching project wins so a nested checkout is not
    served its parent's environment.

    **Stale is withheld rather than offered**, which is the whole reason this asks
    `is_current` instead of just looking. Handing back an interpreter built from a
    declaration that has moved on is how a delegation reports a passing suite, at exit 0,
    against the wrong dependency versions -- and exit 0 is the one thing ADR-0007 tells
    every reader downstream to believe. Absent is a state the model can report and
    `--doctor` explains; a false pass is neither.
    """
    return _covering(cfg, workdir, current=True)


def stale_for(cfg: Config, workdir: str | None) -> dict[str, Any] | None:
    """The record covering this workdir that `provisioned_for` withholds as stale, or None.

    Withholding makes a stale build look exactly like "never provisioned", to the model and
    to the caller alike. This is how the withholding is said out loud instead.
    """
    return _covering(cfg, workdir, current=False)


def _covering(cfg: Config, workdir: str | None, *, current: bool) -> dict[str, Any] | None:
    """The longest-matching record for this workdir whose `is_current` equals `current`."""
    if workdir is None:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for _venv, record in discover(resolve_home(cfg)):
        # A record that is missing or names no project is never current, and not stale
        # either: there is no project to match it against.
        if record is None or not record.get("project") or is_current(record) != current:
            continue
        project = str(record["project"]).rstrip("/")
        if workdir == project or workdir.startswith(project + "/"):
            if best is None or len(project) > best[0]:
                best = (len(project), record)
    return best[1] if best else None


def interpreter_for(cfg: Config, workdir: str | None) -> str | None:
    """The absolute interpreter path for this workdir, or None. See `provisioned_for`."""
    record = provisioned_for(cfg, workdir)
    return str(record["interpreter"]) if record else None


def nested_deselect(project_real: str) -> tuple[str, ...]:
    """The pytest node ids this project says cannot run nested, from its own declaration.

    Read at call time rather than recorded at build time, so correcting the list does not
    need a re-provision -- the list is a statement about the sandbox, not about the
    installed dependencies.

    `[tool.delegate-local] nested-deselect` in `pyproject.toml`, because that is where a
    Python project's other tool configuration already lives and a new dotfile per project
    is a worse trade. Unreadable or malformed is an empty list rather than an error: this
    runs on the path of every `run_bash` call, and refusing a shell command over a
    misspelt table would be a far worse failure than running the test that cannot pass.
    """
    source = Path(project_real) / "pyproject.toml"
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    table = data.get("tool", {}).get("delegate-local", {})
    if not isinstance(table, dict):
        return ()
    listed = table.get("nested-deselect", ())
    if not isinstance(listed, list):
        return ()
    return tuple(str(item) for item in listed if str(item).strip())


def addopts_for(deselect: Sequence[str]) -> str:
    """`--deselect` per node id, as one `PYTEST_ADDOPTS` value.

    `shlex.quote` because pytest splits that variable the way a shell would, so a node id
    carrying a space -- a parametrised case can -- would otherwise become two arguments.
    """
    return " ".join(f"--deselect {shlex.quote(node)}" for node in deselect)


def sandbox_env(cfg: Config, workdir: str | None) -> dict[str, str]:
    """What a sandboxed command is told about its interpreter, or nothing at all.

    `SANDBOX_ENV_NAME` for the interpreter, and `PYTEST_ADDOPTS` for the tests the project
    says cannot run nested. The second is applied here rather than left to the model
    because forgetting it produces a **real** non-zero exit from a test that cannot pass
    inside `--unshare-all` -- a false failure that looks exactly as trustworthy as a true
    one, which is ADR-0007's problem inverted. The server does not touch the model's
    command to do it: `run_bash` takes an opaque shell string, so appending arguments would
    mean parsing shell, and the `--setenv` lands in the argv the transcript records so the
    deselect is answerable from the record.

    An absent name rather than an empty value: a shell expands an unset variable to the
    empty string, so `$DELEGATE_PYTHON -m pytest` would run `-m pytest` as a command and
    fail with something unrelated to the actual cause.
    """
    record = provisioned_for(cfg, workdir)
    if record is None:
        return {}
    env = {SANDBOX_ENV_NAME: str(record["interpreter"])}
    addopts = addopts_for(nested_deselect(str(record["project"])))
    if addopts:
        env["PYTEST_ADDOPTS"] = addopts
    return env


def build_env() -> dict[str, str]:
    """The environment `pip` is run with, with the operator's own pip configuration cut out.

    `PIP_CONFIG_FILE=/dev/null` because a `pip.conf` may carry an index URL with credentials
    embedded in it, and nothing scans the finished tree at runtime -- the secret scan skips
    it by design (ADR-0062), so the guarantee has to be made here, at build time, where it
    can still be made at all. Same reasoning as the sandbox binding `uv`'s binary and
    leaving its cache outside.
    """
    env = dict(os.environ)
    env["PIP_CONFIG_FILE"] = os.devnull
    env.pop("PIP_INDEX_URL", None)
    env.pop("PIP_EXTRA_INDEX_URL", None)
    return env


def _run(argv: list[str], *, out) -> None:
    """One build step, with its real exit code honoured and its output shown on failure."""
    proc = subprocess.run(
        argv, capture_output=True, stdin=subprocess.DEVNULL, text=True,
        timeout=_BUILD_TIMEOUT, env=build_env(), check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        print("\n".join(f"      {line}" for line in tail), file=out)
        raise ProvisionError(f"{argv[0]} exited {proc.returncode}")


def denylist_matches(cfg: Config, venv: str) -> list[str]:
    """Denylist matches inside a finished environment, reported and never covered.

    Nothing covers this tree at runtime, so this is the only place the question is asked.
    Ordinary library files match -- measured on this repository, thirteen of them, including
    `certifi/cacert.pem` and `keyring/credentials.py` -- so a match is not a refusal. It is
    a number an operator can look at, and `--doctor` reports it on every run.

    **The opaque list is pruned here exactly as the scan prunes it**, and that is what makes
    the number comparable. Without it this walks into every `__pycache__` and counts the
    compiled twin of each match: 44 on this repository against the 13 the scan itself would
    have covered. Two true answers to different questions, and the one worth reporting is
    the scan's, since this check exists to say what covering was skipped.
    """
    globs = paths.load_secret_globs(cfg)
    opaque = sandbox.load_opaque_globs(cfg)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(venv, followlinks=False):
        kept: list[str] = []
        for name in dirnames:
            full = posixpath.join(dirpath.replace(os.sep, "/"), name)
            # `sandbox._dir_match` rather than `secret_match`, because a directory glob is
            # written `__pycache__/**` and that matches nothing against the directory's own
            # path -- it needs the probe suffix the scan appends. Reused rather than
            # rewritten for the reason `doctor.py` reuses the server's helpers: a second
            # reading of one list is free to disagree with the first.
            if sandbox._dir_match(full, globs) is not None:
                found.append(full)  # matched: reported, and not descended into
            elif sandbox._dir_match(full, opaque) is None:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            full = posixpath.join(dirpath.replace(os.sep, "/"), name)
            if paths.secret_match(full, globs) is not None:
                found.append(full)
    return found


def build(cfg: Config, project_real: str, *, out) -> str:
    """Build one environment and record what it was built from. Returns the venv directory."""
    home = resolve_home(cfg)
    sandbox.ensure_home(home)
    venv = venv_dir(home, project_real)

    # The working environment is moved aside, never deleted first, so a failed rebuild --
    # network down, a bad pin -- puts it back rather than leaving no interpreter at all.
    # Aside rather than building the new one aside: a virtualenv writes its own absolute
    # path into its scripts, so a tree built at one path and renamed to another is broken.
    previous: str | None = None
    if os.path.exists(venv):
        previous = f"{venv}.previous"
        if os.path.exists(previous):
            _remove(previous)  # left by an interrupted run; the live one is `venv`
        print(f"  moving the existing environment aside while {venv} is rebuilt", file=out)
        _move(venv, previous)
    try:
        _build_into(project_real, venv, out=out)
    except BaseException:
        if os.path.exists(venv):
            _remove(venv)
        if previous is not None:
            _move(previous, venv)
            print("  the build failed, so the previous environment is back in place", file=out)
        raise
    if previous is not None:
        _remove(previous)
    return venv


def _remove(path: str) -> None:
    try:
        shutil.rmtree(path)
    except OSError as e:
        raise ProvisionError(f"could not remove {path}: {e.strerror or e}") from e


def _move(src: str, dst: str) -> None:
    try:
        os.rename(src, dst)
    except OSError as e:
        raise ProvisionError(f"could not move {src} to {dst}: {e.strerror or e}") from e


def _build_into(project_real: str, venv: str, *, out) -> None:
    """The build steps and the record, into `venv`. Raises `ProvisionError` on any failure."""
    print(f"  building {venv}", file=out)
    _run([sys.executable, "-m", "venv", venv], out=out)

    python = interpreter_path(venv)
    target = install_target(project_real)
    print(f"  installing {target}", file=out)
    _run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], out=out)
    _run([python, "-m", "pip", "install", "--quiet", "--editable", target], out=out)

    record = {
        "project": project_real,
        "interpreter": python,
        "dependency_hash": dependency_hash(project_real),
        # A copy, so the next rebuild can show what changed rather than only that it did.
        "declarations": declarations(project_real),
        "hash_sources": [
            n for n in HASH_SOURCES if os.path.exists(posixpath.join(project_real, n))
        ],
        "install_target": target,
        "base_python": sys.executable,
        "provisioned": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        (Path(venv) / RECORD_NAME).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as e:
        raise ProvisionError(f"could not write the build record: {e.strerror or e}") from e


def declarations(project_real: str) -> dict[str, str]:
    """The text of each dependency declaration the project has, newlines normalised."""
    found: dict[str, str] = {}
    for name in HASH_SOURCES:
        try:
            raw = (Path(project_real) / name).read_bytes()
        except OSError:
            continue
        found[name] = raw.replace(b"\r\n", b"\n").decode("utf-8", "replace")
    return found


def declaration_change(project_real: str, record: dict[str, Any] | None) -> str | None:
    """What changed in the declaration since `record` was built, or None if nothing did.

    The build runs the project's own backend on the host, and a delegation can edit the
    declaration, so a rebuild from a changed one is shown before it runs (R5). A record from
    before copies were kept can only say *that* it changed.
    """
    if record is None or dependency_hash(project_real) == record.get("dependency_hash"):
        return None
    before = record.get("declarations")
    if not isinstance(before, dict):
        return (
            "The declaration has changed since the last build, and that build kept no copy "
            "of it, so the change cannot be shown.\n"
        )
    now = declarations(project_real)
    lines: list[str] = []
    for name in HASH_SOURCES:
        old, new = str(before.get(name, "")), now.get(name, "")
        if old != new:
            lines += difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"{name} (last build)", tofile=f"{name} (now)",
            )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def run(cfg: Config, given: str, *, out, yes: bool = False) -> int:
    """Validate the project path, build, and report. Returns an exit code."""
    try:
        project_real = paths.resolve_workdir(cfg, given)
    except PathRefused as e:
        print(f"{e}", file=out)
        return 1

    print(f"delegate-local provision {project_real}", file=out)
    change = declaration_change(
        project_real, read_record(venv_dir(resolve_home(cfg), project_real))
    )
    if change is not None:
        print("\nThe dependency declaration changed since the last build:\n", file=out)
        print(change, file=out)
        if not yes:
            print(
                "Building runs the project's own build backend on this machine, outside the "
                "sandbox, and a delegation can edit these files. Read the change above, then "
                "re-run with --yes to build from it.",
                file=out,
            )
            return 1
    try:
        venv = build(cfg, project_real, out=out)
    except ProvisionError as e:
        # Not "nothing usable was left behind" any more: a previous environment is put
        # back, and `build` has already said so when it was.
        print(f"\nThe build failed: {e}", file=out)
        return 1
    except subprocess.TimeoutExpired:
        print(f"\nThe build exceeded {_BUILD_TIMEOUT:.0f}s and was abandoned.", file=out)
        return 1

    matches = denylist_matches(cfg, venv)
    print(f"\n  interpreter: {interpreter_path(venv)}", file=out)
    print(f"  record:      {posixpath.join(venv, RECORD_NAME)}", file=out)
    print(f"  denylist matches inside it: {len(matches)}", file=out)
    nested = nested_deselect(project_real)
    print(f"  tests declared unable to run nested: {len(nested)}", file=out)
    for node in nested:
        print(f"    - {node}", file=out)
    print(
        "\nThis tree is bound read-only into the sandbox and is the one tree the secret\n"
        "scan skips rather than covers, so those matches are reported here and by\n"
        "--doctor rather than mounted over -- covering them would break the environment\n"
        "(ADR-0062). A delegation reaches the interpreter by that absolute path.",
        file=out,
    )
    return 0


def main(*, argv: list[str] | None = None, out=None) -> int:
    """Refuse where the answers would be meaningless, then provision one project."""
    stream = out if out is not None else sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)

    if os.name != "posix":
        print(
            "provision builds a POSIX virtualenv the sandbox will bind, so it cannot run "
            "on\nWindows. Run it where the server runs -- inside WSL, through the same "
            "interpreter\nthe MCP registration names.",
            file=sys.stderr,
        )
        return 2

    yes = "--yes" in args
    rest = [a for a in args if a not in ("provision", "--yes")]
    if len(rest) != 1:
        print(
            "usage: claude-delegate-local-mcp provision [--yes] <project>\n\n"
            "One project directory, which must resolve inside a configured workdir root.\n"
            "A pasted Windows path is accepted and translated. --yes builds from a\n"
            "dependency declaration that changed since the last build, after it is shown.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = config.load()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1

    return run(cfg, rest[0], out=stream, yes=yes)
