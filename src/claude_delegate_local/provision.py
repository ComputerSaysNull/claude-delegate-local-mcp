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

import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tomllib
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

# What the dependency hash is taken over. Deliberately the whole file rather than the
# dependency tables inside it: the check is asymmetric, and hashing coarsely errs the safe
# way. A false "stale" costs one re-provision; a false "fresh" means a delegation tests
# against the wrong dependencies and returns the clean exit code ADR-0007 says to trust.
HASH_SOURCE = "pyproject.toml"

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


def dependency_hash(project_real: str) -> str | None:
    """A digest of the project's dependency declaration, or None if it has none."""
    source = Path(project_real) / HASH_SOURCE
    try:
        raw = source.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


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


def interpreter_for(cfg: Config, workdir: str | None) -> str | None:
    """The provisioned interpreter covering this workdir, or None. Never a stale one.

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
    if workdir is None:
        return None
    best: tuple[int, str] | None = None
    for _venv, record in discover(resolve_home(cfg)):
        if not is_current(record):
            continue
        assert record is not None  # is_current rejects None
        project = str(record["project"]).rstrip("/")
        if workdir == project or workdir.startswith(project + "/"):
            if best is None or len(project) > best[0]:
                best = (len(project), str(record["interpreter"]))
    return best[1] if best else None


def sandbox_env(cfg: Config, workdir: str | None) -> dict[str, str]:
    """`SANDBOX_ENV_NAME` pointing at the interpreter, or nothing at all.

    An absent name rather than an empty value: a shell expands an unset variable to the
    empty string, so `$DELEGATE_PYTHON -m pytest` would run `-m pytest` as a command and
    fail with something unrelated to the actual cause.
    """
    python = interpreter_for(cfg, workdir)
    return {SANDBOX_ENV_NAME: python} if python else {}


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
        argv, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        env=build_env(), check=False,
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
    """
    globs = paths.load_secret_globs(cfg)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(venv, followlinks=False):
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            if paths.secret_match(full.replace(os.sep, "/"), globs) is not None:
                found.append(full)
    return found


def build(cfg: Config, project_real: str, *, out) -> str:
    """Build one environment and record what it was built from. Returns the venv directory."""
    home = resolve_home(cfg)
    sandbox.ensure_home(home)
    venv = venv_dir(home, project_real)

    if os.path.exists(venv):
        print(f"  replacing the existing environment at {venv}", file=out)
        shutil.rmtree(venv)

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
        "hash_source": HASH_SOURCE,
        "install_target": target,
        "base_python": sys.executable,
        "provisioned": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (Path(venv) / RECORD_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return venv


def run(cfg: Config, given: str, *, out) -> int:
    """Validate the project path, build, and report. Returns an exit code."""
    try:
        project_real = paths.resolve_workdir(cfg, given)
    except PathRefused as e:
        print(f"{e}", file=out)
        return 1

    print(f"delegate-local provision {project_real}", file=out)
    try:
        venv = build(cfg, project_real, out=out)
    except ProvisionError as e:
        print(f"\nNothing usable was left behind: {e}", file=out)
        return 1
    except subprocess.TimeoutExpired:
        print(f"\nThe build exceeded {_BUILD_TIMEOUT:.0f}s and was abandoned.", file=out)
        return 1

    matches = denylist_matches(cfg, venv)
    print(f"\n  interpreter: {interpreter_path(venv)}", file=out)
    print(f"  record:      {posixpath.join(venv, RECORD_NAME)}", file=out)
    print(f"  denylist matches inside it: {len(matches)}", file=out)
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

    rest = [a for a in args if a != "provision"]
    if len(rest) != 1:
        print(
            "usage: claude-delegate-local-mcp provision <project>\n\n"
            "One project directory, which must resolve inside a configured workdir root.\n"
            "A pasted Windows path is accepted and translated.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = config.load()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1

    return run(cfg, rest[0], out=stream)
