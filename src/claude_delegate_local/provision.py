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

**Two phases, so no project code runs where the network is.** The host keeps the network
and downloads the declared wheels with `pip download`, reading the declaration with
`tomllib` and never running the project's own backend; that backend runs later, inside the
sandbox where the network is denied, so anything anyone who can write the project controls
cannot exfiltrate during the build.

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

import difflib
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # POSIX only. Absent on Windows, where the test suite runs but the server never does.
    import fcntl
except ImportError:  # pragma: no cover -- exercised by the Windows leg of CI
    fcntl = None  # type: ignore[assignment]

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

# The one file a project with no package metadata declares its dependencies in. Consulted
# only then: package metadata wins, and a stray requirements.txt is ignored, for the hash
# and the install alike -- the build reads only what the hash covers.
REQUIREMENTS_NAME = "requirements.txt"

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

# What a PEP 517 backend is asked for, and the requirement list that installs it, when
# `[build-system]` names neither. Mirrored by `static_requirements`'s default, so the query
# installs the same thing phase 1 already downloaded.
_DEFAULT_BUILD_REQUIRES = ("setuptools>=40.8.0",)
_DEFAULT_BACKEND = "setuptools.build_meta:__legacy__"

# The marker line the backend query prints, and the script that runs inside the sandbox to
# produce it. A PEP 517 backend reports extra build requirements dynamically from
# `get_requires_for_build_editable()` -- code the host must not run -- so the query is the
# only way to learn them without running project code where the network is.
_BACKEND_REQUIRES_MARKER = "DELEGATE_BUILD_REQUIRES="
_BACKEND_QUERY_SCRIPT = """\
import importlib, json, os, sys
backend = json.loads(sys.argv[1])
paths = json.loads(sys.argv[2])
for entry in paths:
    sys.path.insert(0, entry if os.path.isabs(entry) else os.path.join(os.getcwd(), entry))
if ":" in backend:
    module, _, attrs = backend.partition(":")
    obj = importlib.import_module(module)
    for attr in attrs.split('.'):
        if attr:
            obj = getattr(obj, attr)
else:
    obj = importlib.import_module(backend)
if hasattr(obj, "get_requires_for_build_editable"):
    reqs = obj.get_requires_for_build_editable({})
elif hasattr(obj, "get_requires_for_build_wheel"):
    reqs = obj.get_requires_for_build_wheel({})
else:
    reqs = []
print("DELEGATE_BUILD_REQUIRES=" + json.dumps(list(reqs)))
"""


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


def staging_dir(home: str, project_real: str) -> str:
    """The staging directory one build downloads wheels into, derived not configured.

    A hidden sibling of the venv, so the sandbox's read-only bind of the provisioned root
    already covers it: the wheels need no bind of their own, and `pip install --no-index
    --find-links` reads them from there. The dot-prefix is what keeps it out of a casual
    listing of the provisioned root.
    """
    return posixpath.join(provisioned_root(home), "." + venv_name(project_real) + ".staging")


def build_lock_path(home: str, project_real: str) -> str:
    """The lock file one project's build holds, derived like the venv name."""
    return posixpath.join(provisioned_root(home), "." + venv_name(project_real) + ".lock")


def acquire_build_lock(home: str, project_real: str) -> int:
    """A non-blocking exclusive lock over one project's build, or refuse.

    Returns the open file descriptor; closing it releases the lock. A second build of the
    same project while one holds the lock is refused rather than queued -- a build is
    minutes long, and an operator is better told the first one is still running than left to
    wonder why a second is stacking behind it.
    """
    if fcntl is None:
        raise ProvisionError("build locking needs fcntl, which is not available here")
    path = build_lock_path(home, project_real)
    os.makedirs(posixpath.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ProvisionError("a build for this project is already running") from None
    return fd


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

    A project declaring no package metadata reads `requirements.txt` instead, hashed the
    same way -- newlines normalised, since that is what its build installs from. Package
    metadata always wins: a stray requirements.txt beside it is not read at all, so it
    cannot make a package project look stale.
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
        # No package metadata: requirements.txt is the declaration, and it alone is hashed,
        # since that is what the build reads. Missing it means there is no declaration.
        try:
            raw = (Path(project_real) / REQUIREMENTS_NAME).read_bytes()
        except OSError:
            return None
        parts.append((REQUIREMENTS_NAME, raw.replace(b"\r\n", b"\n")))
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


def _declaration_names(project_real: str) -> tuple[str, ...]:
    """The declaration files this build reads from, package metadata first.

    Package metadata outranks requirements.txt: a project declaring any of the package
    sources is a package project, and a stray requirements.txt is not read. A project
    declaring none reads requirements.txt when it exists, and nothing when it does not.
    """
    found = tuple(
        name for name in HASH_SOURCES if (Path(project_real) / name).exists()
    )
    if found:
        return found
    if (Path(project_real) / REQUIREMENTS_NAME).exists():
        return (REQUIREMENTS_NAME,)
    return ()


def static_requirements(project_real: str) -> list[str]:
    """The dependency list, read without running any project code.

    This is what makes the two-phase build possible: the host reads the declaration with
    `tomllib` and keeps the network only for downloading wheels, and the project's own build
    backend never runs where the network is. A requirements-only project's non-blank,
    non-comment lines; a package project's `[project].dependencies`, plus the `dev` extra
    when `install_target` adds it, plus `[build-system].requires`.
    """
    if _declaration_names(project_real) == (REQUIREMENTS_NAME,):
        reqs = _validate_requirements(Path(project_real) / REQUIREMENTS_NAME)
        for req in reqs:
            _refuse_unverifiable(req)
        return reqs
    return _package_requirements(project_real)


def _package_requirements(project_real: str) -> list[str]:
    """A package project's requirements, read statically from its `pyproject.toml`.

    Refused when the declaration cannot be read statically: a `[project].dynamic` entry for
    a dependency table, or setup.py/setup.cfg with no `[project]` table -- either way the
    build would have to run the project's own code to learn what to install, which is
    exactly what phase 1 exists to avoid.
    """
    source = Path(project_real) / "pyproject.toml"
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    if _declares_via_setup(project_real) and "project" not in data:
        raise ProvisionError(
            f"{project_real} declares its dependencies in setup.py/setup.cfg, which phase 1 "
            "would have to run to read. Declare them statically in pyproject.toml's "
            "[project] table or in requirements.txt."
        )
    project = data.get("project", {})
    if not isinstance(project, dict):
        project = {}
    dynamic = project.get("dynamic", ())
    if isinstance(dynamic, list) and any(
        name in ("dependencies", "optional-dependencies") for name in dynamic
    ):
        raise ProvisionError(
            f"{project_real} declares dependencies as dynamic, which phase 1 cannot read "
            "without running the project's own code. Declare them statically in "
            "pyproject.toml's [project] table or in requirements.txt."
        )
    reqs: list[str] = []
    deps = project.get("dependencies", ())
    if isinstance(deps, list):
        reqs.extend(str(dep) for dep in deps)
    if "dev" in _extras(project_real):
        optional = project.get("optional-dependencies", {})
        dev = optional.get("dev", ()) if isinstance(optional, dict) else ()
        if isinstance(dev, list):
            reqs.extend(str(dep) for dep in dev)
    build = data.get("build-system")
    requires: tuple[str, ...] = _DEFAULT_BUILD_REQUIRES
    if isinstance(build, dict):
        listed = build.get("requires")
        if isinstance(listed, list):
            requires = tuple(str(req) for req in listed)
    reqs.extend(requires)
    for req in reqs:
        _refuse_unverifiable(req)
    return reqs


def _declares_via_setup(project_real: str) -> bool:
    """Does the project declare itself in setup.py/setup.cfg rather than `[project]`?"""
    return (Path(project_real) / "setup.py").exists() or (
        Path(project_real) / "setup.cfg"
    ).exists()


def _build_system(project_real: str) -> dict[str, Any]:
    """The `[build-system]` table, or {} when it is absent or unreadable."""
    source = Path(project_real) / "pyproject.toml"
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    build = data.get("build-system")
    return build if isinstance(build, dict) else {}


def _build_requires(build: dict[str, Any]) -> tuple[str, ...]:
    """The build-system requires, or the default when the table names none."""
    listed = build.get("requires")
    if isinstance(listed, list):
        return tuple(str(req) for req in listed)
    return _DEFAULT_BUILD_REQUIRES


_ARCHIVE_SUFFIXES = (
    ".whl", ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tar.xz", ".txz"
)


def _refuse_unverifiable(requirement: str) -> None:
    """Refuse a requirement the build cannot download and record as one thing.

    Only a plain name, optional extras, version specifiers and a marker are acceptable. A
    URL or path -- a leading `-`/`.`/`/`/`~`, or `@`/`://`/`file:` -- is a way to send data
    out of the sandbox or to install what the dependency hash does not cover, so it is
    refused rather than downloaded on the host's network. So is a name ending like an
    archive: pip reads one that exists as a local file, and builds a local sdist on the
    host.
    """
    if (
        requirement.startswith(("-", ".", "/", "~"))
        or "@" in requirement
        or "://" in requirement
        or "file:" in requirement
        or requirement.lower().endswith(_ARCHIVE_SUFFIXES)
    ):
        raise ProvisionError(
            f"refused requirement {requirement!r}: a URL or path is a way to send data out "
            "or to install what the dependency hash does not cover"
        )


def _validate_requirements(requirements: Path) -> list[str]:
    """A requirements file's requirement lines, refusing a pip option line.

    An option line pulls in what the hash does not cover -- `-r`/`-c` another file, `-e` a
    checkout -- or points pip at an index the operator's configuration cut-out was meant to
    exclude (`-i`, `--index-url`, `--extra-index-url`, `-f`, `--find-links`). Either breaks
    the guarantee the build makes, so the build is refused rather than made against
    something it will not record. Comments and blank lines are fine.
    """
    try:
        text = requirements.read_text(encoding="utf-8")
    except OSError as e:
        raise ProvisionError(
            f"could not read {REQUIREMENTS_NAME}: {e.strerror or e}"
        ) from e
    lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            raise ProvisionError(
                f"{REQUIREMENTS_NAME}:{lineno}: option line refused: {stripped}\n"
                "(an included file would escape the dependency hash, and an index option "
                "would bypass the cut-out of the operator's pip configuration)"
            )
        lines.append(stripped)
    return lines


def install_request(
    cfg: Config, project_real: str, venv: str, staging: str
) -> sandbox.SandboxRequest:
    """The sandboxed install for one build, as a request, so tests check it without bwrap.

    The command installs into `venv` from `staging` only, with no index and no network. For a
    package project the workdir is the project, since the editable build may write into it;
    for a requirements project there is no project code, so no workdir is bound.
    """
    python = interpreter_path(venv)
    if _declaration_names(project_real) == (REQUIREMENTS_NAME,):
        args = ["-r", posixpath.join(staging, REQUIREMENTS_NAME)]
        workdir = None
    else:
        args = ["--editable", install_target(project_real)]
        workdir = project_real
    command = shlex.join([
        python, "-m", "pip", "install",
        "--no-index", "--no-cache-dir", "--find-links", staging, *args,
    ])
    return sandbox.SandboxRequest(
        command=command,
        home=sandbox.resolve_home(cfg),
        workdir=workdir,
        network=False,
        writable_binds=(venv,),
        env={**sandbox.resolve_env(cfg), "PIP_CONFIG_FILE": os.devnull},
    )


def backend_query_request(
    cfg: Config, project_real: str, staging: str, venv: str
) -> sandbox.SandboxRequest:
    """The sandboxed query that asks the build backend for its extra build requirements.

    A PEP 517 backend reports extra build requirements dynamically from
    `get_requires_for_build_editable()` -- code the host must not run -- so the query runs
    here, in the sandbox with the network denied, and only the requirement names cross back
    to the host, past the same plain-name check as the declaration. The command first
    installs `[build-system].requires` into a scratch target with the venv's own
    interpreter, then runs a script that imports the backend and asks it.
    """
    build = _build_system(project_real)
    requires = _build_requires(build)
    backend = str(build.get("build-backend") or _DEFAULT_BACKEND)
    listed = build.get("backend-path")
    backend_path = tuple(str(p) for p in listed) if isinstance(listed, list) else ()
    python = interpreter_path(venv)
    install = shlex.join([
        python, "-m", "pip", "install",
        "--no-index", "--no-cache-dir", "--find-links", staging,
        "--target", "/tmp/delegate-backend", *requires,
    ])
    query = shlex.join([
        python, "-c", _BACKEND_QUERY_SCRIPT,
        json.dumps(backend), json.dumps(list(backend_path)),
    ])
    return sandbox.SandboxRequest(
        command=f"{install} && {query}",
        home=sandbox.resolve_home(cfg),
        workdir=project_real,
        network=False,
        env={
            **sandbox.resolve_env(cfg),
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONPATH": "/tmp/delegate-backend",
        },
    )


def parse_backend_requires(stdout: str) -> list[str]:
    """The extra build requirements the backend reported, from its one marker line.

    Anything else -- no marker, unparseable JSON, not a list of strings -- is refused with
    what came back, because a build cannot verify what it cannot read as a requirement list.
    """
    for line in stdout.splitlines():
        if not line.startswith(_BACKEND_REQUIRES_MARKER):
            continue
        payload = line[len(_BACKEND_REQUIRES_MARKER):]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            raise ProvisionError(
                f"the backend query reported requirements it could not parse: "
                f"{_truncate(payload)}"
            ) from None
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ProvisionError(
                f"the backend query reported requirements that are not a list of strings: "
                f"{_truncate(payload)}"
            )
        return parsed
    raise ProvisionError(
        f"the backend query printed no {_BACKEND_REQUIRES_MARKER!r} line; got: "
        f"{_truncate(stdout)}"
    )


def _truncate(text: str, limit: int = 300) -> str:
    """Shorten what came back so a refusal names it without flooding the build log."""
    return text if len(text) <= limit else text[:limit] + "..."


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


def _run(argv: list[str], *, out, cwd: str | None = None) -> None:
    """One build step, with its real exit code honoured and its output shown on failure."""
    proc = subprocess.run(
        argv, capture_output=True, stdin=subprocess.DEVNULL, text=True,
        timeout=_BUILD_TIMEOUT, env=build_env(), check=False, cwd=cwd,
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

    # One build per project at a time. Non-blocking, so a second operator action fails fast
    # rather than stacking behind a build that may take minutes.
    lock = acquire_build_lock(home, project_real)
    try:
        # The working environment is moved aside, never deleted first, so a failed rebuild --
        # network down, a bad pin -- puts it back rather than leaving no interpreter. The old
        # one moves, not the new one: a virtualenv writes its absolute path into its scripts,
        # so a tree built at one path and renamed is broken.
        previous: str | None = None
        if os.path.exists(venv):
            previous = f"{venv}.previous"
            if os.path.exists(previous):
                _remove(previous)  # left by an interrupted run; the live one is `venv`
            print(f"  moving the existing environment aside while {venv} is rebuilt", file=out)
            _move(venv, previous)
        try:
            _build_into(cfg, project_real, venv, home, out=out)
        except BaseException:
            if os.path.exists(venv):
                _remove(venv)
            if previous is not None:
                _move(previous, venv)
                print("  the build failed, so the previous environment is back in place", file=out)
            raise
        if previous is not None:
            _remove(previous)
    finally:
        os.close(lock)
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


def _build_into(cfg: Config, project_real: str, venv: str, home: str, *, out) -> None:
    """The two-phase build and the record, into `venv`. Raises `ProvisionError` on failure.

    Phase 1 runs on the host, with the network, but never runs project code: it makes the
    venv and downloads wheels. Phase 2 runs inside the sandbox, where the network is denied,
    and is where the project's own build backend finally runs.
    """
    print(f"  building {venv}", file=out)
    staging = staging_dir(home, project_real)
    try:
        _run([sys.executable, "-m", "venv", venv], out=out)
        _download_wheels(project_real, staging, out=out)

        # A package project's backend may report extra build requirements dynamically, so
        # they are queried and staged before the install; a requirements-only project has no
        # backend to ask and skips this round entirely.
        if _declaration_names(project_real) != (REQUIREMENTS_NAME,):
            _backend_round(cfg, project_real, venv, staging, out=out)

        _install_in_sandbox(
            cfg, install_request(cfg, project_real, venv, staging), out=out
        )

        python = interpreter_path(venv)
        record = {
            "project": project_real,
            "interpreter": python,
            "dependency_hash": dependency_hash(project_real),
            # A copy, so the next rebuild can show what changed rather than only that it did.
            "declarations": declarations(project_real),
            "hash_sources": list(_declaration_names(project_real)),
            "install_target": (
                install_target(project_real)
                if _declaration_names(project_real) != (REQUIREMENTS_NAME,)
                else str(Path(project_real) / REQUIREMENTS_NAME)
            ),
            "base_python": sys.executable,
            "provisioned": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            (Path(venv) / RECORD_NAME).write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as e:
            raise ProvisionError(f"could not write the build record: {e.strerror or e}") from e
    finally:
        _remove_staging(staging)


def _download_wheels(project_real: str, staging: str, *, out) -> None:
    """Phase 1: download every requirement's wheels, on the host, with no project code.

    `--only-binary :all:` so nothing needs the project's own backend or a source build on
    the host -- a wheel is the one shape `pip install --no-index` inside the sandbox can
    consume. `build_env()` cuts the operator's pip configuration out, so an index URL with
    credentials cannot be recorded in what the sandbox then reads.
    """
    if os.path.exists(staging):
        _remove(staging)
    os.makedirs(staging)
    reqs = static_requirements(project_real)
    if _declaration_names(project_real) == (REQUIREMENTS_NAME,):
        (Path(staging) / REQUIREMENTS_NAME).write_text(
            "\n".join(reqs) + "\n", encoding="utf-8"
        )
    _download_requirements(staging, reqs, out=out)


def _download_requirements(staging: str, reqs: Sequence[str], *, out) -> None:
    """Download the given requirements' wheels into the existing staging directory.

    Shared by the declaration round and the backend round, so the second set of wheels lands
    beside the first in the one `--find-links` source phase 2 reads. Run from inside the
    staging directory, so no argument can name a file pip would read as a local archive and
    build on the host.
    """
    if not reqs:
        return
    print(f"  downloading wheels into {staging}", file=out)
    _run([sys.executable, "-m", "pip", "download",
          "--only-binary", ":all:", "--dest", staging, *reqs], out=out, cwd=staging)


def _requirement_name(requirement: str) -> str:
    """A requirement's distribution name, up to its first extra, marker or version bound."""
    m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    return m.group(0) if m else requirement


def _static_names(project_real: str) -> frozenset[str]:
    """The distribution names phase 1 already downloaded, for the backend round's dedup."""
    return frozenset(_requirement_name(r) for r in static_requirements(project_real))


def _backend_round(cfg: Config, project_real: str, venv: str, staging: str, *, out) -> None:
    """Ask the build backend for its dynamic build requirements, then stage them.

    The backend runs only in the sandbox with the network denied; what crosses back to the
    host is a list of requirement names that pass the same plain-name check as the
    declaration, so only the package index sees them. A name already staged from the
    declaration is not downloaded twice.
    """
    print("  asking the build backend for its extra requirements", file=out)
    result = _run_sandbox(
        cfg, backend_query_request(cfg, project_real, staging, venv),
        out=out, what="backend query",
    )
    dynamic = parse_backend_requires(result.stdout)
    for req in dynamic:
        _refuse_unverifiable(req)
    remaining = [
        req for req in dynamic
        if _requirement_name(req) not in _static_names(project_real)
    ]
    if remaining:
        _download_requirements(staging, remaining, out=out)


def _run_sandbox(
    cfg: Config, req: sandbox.SandboxRequest, *, out, what: str
) -> sandbox.SandboxResult:
    """Run one sandboxed build step, with its real exit code honoured and its tail shown.

    Shared by the install and the backend query: both run project-controlled code only in
    the sandbox where the network is denied, and both fail the build on a non-zero or
    timed-out run rather than on a message.
    """
    try:
        result = sandbox.run(
            replace(cfg, run_bash_timeout=int(_BUILD_TIMEOUT)), req
        )
    except (sandbox.SandboxUnavailable, sandbox.SecretShadowIncomplete) as e:
        raise ProvisionError(str(e)) from e
    if result.timed_out or result.exit_code != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        print("\n".join(f"      {line}" for line in tail), file=out)
        if result.timed_out:
            raise ProvisionError(
                f"the sandboxed {what} exceeded {_BUILD_TIMEOUT:.0f}s and was abandoned"
            )
        raise ProvisionError(f"the {what} inside the sandbox exited {result.exit_code}")
    return result


def _install_in_sandbox(cfg: Config, req: sandbox.SandboxRequest, *, out) -> None:
    """Phase 2: the install, inside the sandbox with the network denied.

    The project's own build backend -- the code anyone who can write the project controls --
    runs here, where it cannot reach the network, rather than on the host where phase 1's
    download just happened.
    """
    _run_sandbox(cfg, req, out=out, what="pip install")


def _remove_staging(staging: str) -> None:
    """Remove the staging directory, whether the build succeeded or not."""
    if os.path.exists(staging):
        _remove(staging)


def declarations(project_real: str) -> dict[str, str]:
    """The text of each dependency declaration the project has, newlines normalised."""
    found: dict[str, str] = {}
    for name in _declaration_names(project_real):
        try:
            raw = (Path(project_real) / name).read_bytes()
        except OSError:
            continue
        found[name] = raw.replace(b"\r\n", b"\n").decode("utf-8", "replace")
    return found


def declaration_change(project_real: str, record: dict[str, Any] | None) -> str | None:
    """What changed in the declaration since `record` was built, or None if nothing did.

    The build reads the declaration on the host and runs the project's own backend only
    inside the sandbox, and a delegation can edit the declaration, so a rebuild from a
    changed one is shown before it runs. A record keeping no copy can only say *that* it
    changed.
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
    for name in sorted(set(before) | set(now)):
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
                "A delegation can edit these files, and the build installs what they name "
                "into the interpreter every delegation here runs. Read the change above, then "
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
