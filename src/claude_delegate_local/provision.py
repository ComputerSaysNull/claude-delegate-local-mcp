"""`provision <project>`: build the interpreter a delegation runs that project's tests with.

`run_bash` exists so a delegated model can verify its own work by running something, and
ADR-0007 rests the design on the server capturing that command's real exit code. Inside
the sandbox a Python project has no `python` on `PATH`, no `pytest`, and no network to
install them. `sandbox_home` can hold an interpreter: it is bound read-write, persistent,
and outside the workspace, which ADR-0041 makes mandatory rather than tidy.

**This runs server-side, which is where the network is.** A sandboxed command has no
network by design (ADR-0063); provisioning is the step that needs one, so it happens out
here, once, as an operator action. `uv` is no substitute: the sandbox binds only its
binary and leaves its cache outside, so in an empty root with no network it resolves
nothing.

**Outside the workspace, and under `venvs/` rather than `.venv/`.** Both are forced.
Inside a workspace root the secret scan walks the virtualenv and covers `certifi/cacert.pem`,
`keyring/credentials.py` and all of `secretstorage/` with `/dev/null`, breaking the
environment (ADR-0041). And `security/opaque_globs.txt` names `.venv/**` and `venv/**`,
whose matches are *covered* with a tmpfs, so a venv called either would be invisible.
`sandbox.PROVISIONED_DIRNAME` is the one tree the scan skips without covering, and
`build_argv` binds it read-only in exchange (ADR-0062).

**`posixpath`, never `os.path`.** Everything below the WSL boundary is POSIX-only
(`wsl.py` is where paths cross it), and the command refuses to run on Windows -- but its
*tests* run there, and `os.path.join` would hand them backslashes no bind can use.

**The record beside each venv makes this discoverable.** `--doctor` walks the provisioned
root and reads `provision.json`, so no setting has to be kept in step with what was built.
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

# The file recorded beside every provisioned environment, named once for `--doctor` too.
RECORD_NAME = "provision.json"

# What the dependency hash is taken over: every file a build backend reads dependencies
# from, whole rather than just their dependency tables, because the check is asymmetric and
# coarse errs safe. A false "stale" costs one re-provision; a false "fresh" tests against
# the wrong dependencies and returns the clean exit code ADR-0007 says to trust --
# `pyproject.toml` alone would be a false fresh for a project declaring in `setup.cfg`.
HASH_SOURCES = ("pyproject.toml", "setup.cfg", "setup.py")

# Tool tables the build backend never reads, dropped before `pyproject.toml` is hashed, so
# rewording a pytest marker does not withhold a sound interpreter. `delegate-local` is ours:
# `nested-deselect` is read at call time and must not need a re-provision. A denylist, so an
# unknown table still counts: a mistake costs a false "stale", never a false "fresh".
NON_BUILD_TOOL_TABLES = ("pytest", "ruff", "mypy", "coverage", "pyright", "delegate-local")

# The environment name a sandboxed command finds the interpreter under. Absent when nothing
# current is provisioned for the workdir, never empty -- see `sandbox_env`.
SANDBOX_ENV_NAME = "DELEGATE_PYTHON"

# How long a build may take. Generous: a virtualenv plus a dependency resolution over the
# network takes minutes on a cold cache.
_BUILD_TIMEOUT = 1800.0


class ProvisionError(RuntimeError):
    """Anything that stops a build, reported to the operator rather than raised at a caller."""


def venv_name(project_real: str) -> str:
    """A directory name for one project: its basename, plus a digest of its full path.

    The digest is not decoration: two checkouts of one repository -- a worktree, a second
    clone -- share a basename but not dependencies, and a collision would silently hand one
    the other's interpreter.
    """
    digest = hashlib.sha256(project_real.encode("utf-8")).hexdigest()[:8]
    return f"{posixpath.basename(project_real.rstrip('/')) or 'project'}-{digest}"


def venv_dir(home: str, project_real: str) -> str:
    """Where this project's environment lives, derived rather than configured."""
    return posixpath.join(provisioned_root(home), venv_name(project_real))


def interpreter_path(venv: str) -> str:
    """The absolute path a delegation invokes. Absolute, never a `PATH` entry.

    `sandbox.SANDBOX_PATH` stays `/usr/bin:/usr/sbin`: widening it would put one project's
    tools on every command's PATH, and a provisioned venv works inside
    `bwrap --unshare-all` with no PATH change.
    """
    return posixpath.join(venv, "bin", "python")


def _pyproject_bytes(raw: bytes) -> bytes:
    """The bytes `pyproject.toml` hashes to: a canonical form of what the build can read.

    Parsed with `tomllib`, the non-build tool tables dropped, and serialised with sorted
    keys so equivalent TOML hashes identically. Unparseable, it falls back to the raw
    bytes, the safe direction (a false "stale", never a false "fresh").
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

    **Line endings are normalised first, which is correctness, not tidiness.** On a Windows
    checkout every branch switch and fresh clone can flip CRLF and LF, and identical
    content would read as a changed declaration: `interpreter_for` would withhold the
    interpreter, and `run_bash` could run no test, with nothing about the project changed.

    `pyproject.toml` is canonicalised by `_pyproject_bytes`, so rewording a marker under
    `[tool.pytest.ini_options]` does not make a sound environment look stale. The other
    sources stay whole-file, newlines normalised: a real dependency change is a change to
    bytes that are not newlines, so it still moves the digest.

    Every file in `HASH_SOURCES` that exists counts. One that exists and cannot be read makes
    the answer None -- "cannot tell" -- since leaving it out would be a false fresh. A
    project declaring only `pyproject.toml` gets the plain digest of that file alone, so its
    recorded environments stay current.
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
        # Name and length before the bytes, so no two file sets concatenate to one input.
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

    A record of `None` for a directory whose record is missing or unreadable, a state
    `--doctor` must report, not skip: a half-built environment is where a test run would
    pass against nothing.
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

    Both halves are required: the interpreter is there, and the declaration it was built
    from is unchanged. A missing digest on either side is *not* current: "cannot tell" and
    "matches" are different answers, and only one is safe.
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
    anywhere under it, and the longest matching project wins, so a nested checkout is not
    served its parent's environment.

    **Stale is withheld, not offered**, which is why this asks `is_current`. An interpreter
    built from a declaration that has moved on reports a passing suite, at exit 0, against
    the wrong dependency versions -- and exit 0 is what ADR-0007 tells every reader to
    believe. Absent is a state the model can report and `--doctor` explains; a false pass
    is neither.
    """
    return _covering(cfg, workdir, current=True)


def stale_for(cfg: Config, workdir: str | None) -> dict[str, Any] | None:
    """The record covering this workdir that `provisioned_for` withholds as stale, or None.

    Withholding makes a stale build look exactly like "never provisioned"; this is how the
    withholding is said out loud.
    """
    return _covering(cfg, workdir, current=False)


def _covering(cfg: Config, workdir: str | None, *, current: bool) -> dict[str, Any] | None:
    """The longest-matching record for this workdir whose `is_current` equals `current`."""
    if workdir is None:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for _venv, record in discover(resolve_home(cfg)):
        # A record missing or naming no project is neither current nor stale: there is no
        # project to match it against.
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

    Read at call time, not recorded at build time, so correcting the list needs no
    re-provision: it is about the sandbox, not the installed dependencies.

    `[tool.delegate-local] nested-deselect` in `pyproject.toml`, where a Python project's
    other tool configuration lives, rather than a new dotfile per project. Unreadable or
    malformed is an empty list, not an error: this runs on every `run_bash` call, and
    refusing a shell command over a misspelt table is far worse than running a test that
    cannot pass.
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

    `shlex.quote`, because pytest splits that variable as a shell would, so a node id with
    a space -- a parametrised case can have one -- would become two arguments.
    """
    return " ".join(f"--deselect {shlex.quote(node)}" for node in deselect)


def sandbox_env(cfg: Config, workdir: str | None) -> dict[str, str]:
    """What a sandboxed command is told about its interpreter, or nothing at all.

    `SANDBOX_ENV_NAME` for the interpreter, and `PYTEST_ADDOPTS` for the tests the project
    says cannot run nested. The second is applied here, not left to the model, because
    forgetting it gives a **real** non-zero exit from a test that cannot pass inside
    `--unshare-all` -- a false failure as trustworthy-looking as a true one, ADR-0007's
    problem inverted. The model's command is untouched: `run_bash` takes an opaque shell
    string, and appending arguments would mean parsing shell. The `--setenv` lands in the
    argv the transcript records, so the deselect is answerable from the record.

    An absent name, not an empty value: a shell expands an unset variable to the empty
    string too, so `$DELEGATE_PYTHON -m pytest` would run `-m pytest` as a command and fail
    for an unrelated-looking reason.
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

    `PIP_CONFIG_FILE=/dev/null` because a `pip.conf` may carry an index URL with embedded
    credentials, and the secret scan skips the finished tree by design (ADR-0062), so the
    guarantee must be made here, at build time, or not at all -- the reasoning that binds
    only `uv`'s binary and leaves its cache outside.
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
    Ordinary library files match -- `certifi/cacert.pem`, `keyring/credentials.py` -- so a
    match is not a refusal but a number an operator can look at, which `--doctor` reports
    on every run.

    **The opaque list is pruned exactly as the scan prunes it**, which makes the number
    comparable. Otherwise this would walk every `__pycache__` and count each match's
    compiled twin too; the scan's answer is the one worth reporting, since this check says
    what covering was skipped.
    """
    globs = paths.load_secret_globs(cfg)
    opaque = sandbox.load_opaque_globs(cfg)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(venv, followlinks=False):
        kept: list[str] = []
        for name in dirnames:
            full = posixpath.join(dirpath.replace(os.sep, "/"), name)
            # `sandbox._dir_match`, not `secret_match`: a directory glob is written
            # `__pycache__/**`, which matches nothing against the directory's own path
            # without the probe suffix the scan appends. Reused, not rewritten, because a
            # second reading of one list is free to disagree with the first.
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
    # network down, a bad pin -- puts it back rather than leaving no interpreter. The old one
    # moves, not the new one: a virtualenv writes its absolute path into its scripts, so a
    # tree built at one path and renamed is broken.
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
    declaration, so a rebuild from a changed one is shown before it runs. A record keeping
    no copy can only say *that* it changed.
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
        # A previous environment is put back, and `build` has already said so.
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
