"""`--doctor`: check the environment the server assumes, before a delegation depends on it.

The server starts regardless of every fact it needs being wrong. Measured 2026-09-07: with
a nonexistent workspace root, a `bwrap` that is not installed and an endpoint nothing is
listening on, it completed an MCP handshake in about a second and served its full tool
list. `config.load` validates *values* -- that the roots tuple is non-empty, that a
timeout nests inside its parent -- and `registry.load` validates that `models.toml` parses.
Neither asks the filesystem or the network anything. Each fault therefore surfaces later,
inside whichever call first reaches it, as a refusal whose cause is a layer away.

That is what this module exists to close, and the cost of not having it is on record: `uv`
was absent for a whole session, so `toolchain_binds` probed and bound nothing, and the
resulting "a workdir cannot run the tests" was filed as an architectural limit rather than
a missing package. `config.py` had already predicted that exact failure in the help text
for the setting.

**Reuse, not reimplementation.** Every check here calls the same code the server calls, so
a check cannot drift into agreeing with a server that has changed underneath it:
`paths.resolved_roots`, `sandbox.available`, `sandbox.limiter_available`,
`sandbox.probe_toolchain_binds`, `server.probe_entry`, `slots.build_slots` and
`transcript.enabled`. The one thing written fresh is the
`bwrap` *execution* probe, because `sandbox.available` answers only whether the binary is
on `PATH` -- and a `bwrap` that is present but cannot unshare a namespace passes that and
fails everything after it.

**stdout is the report here, unlike every other entry into this package.** `main.run`
keeps stdout for the protocol because Claude Code is on the other end of it; nothing
speaks MCP to a doctor run, so the report goes to stdout and the exit code carries the
verdict for a script.

**Endpoint identity is never printed.** ADR-0029 keeps the address out of
`backend_status`, and a diagnostic that prints it to be helpful would be the leak that
decision exists to prevent. Rows are named by their registry key.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, paths, registry, sandbox, transcript
from .config import Config, ConfigError
from .server import STATUS_OK, BackendCache, probe_entry
from .slots import build_slots
from .wsl import UntranslatablePath

# Verdicts, worst last: the exit code is decided by the worst one seen.
OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

# How long the bwrap execution probe may take. Generous: it is one `true` inside a fresh
# namespace, and a machine slow enough to miss this has a problem worth reporting anyway.
_BWRAP_PROBE_TIMEOUT = 20.0


@dataclass
class Check:
    """One question, its verdict, and what a reader should do about it."""

    name: str
    verdict: str
    detail: str
    remedy: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _fmt(check: Check) -> str:
    mark = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}[check.verdict]
    line = f"  {mark}  [{check.name}] {check.detail}"
    if check.remedy and check.verdict != OK:
        line += f"\n        -> {check.remedy}"
    return line


def check_platform() -> Check:
    """Refuse on Windows, where every answer below would be meaningless.

    Not "must be WSL". The server runs on native Linux too, and there the checks are
    exactly as meaningful. What cannot work is Windows: `bwrap` is Linux-only, the paths
    are POSIX, and a report that passed there would describe a machine the server never
    runs on.
    """
    if os.name == "posix":
        detail = f"posix ({sys.platform})"
        if _is_wsl():
            detail += ", under WSL"
        return Check("platform", OK, detail)
    return Check(
        "platform", FAIL,
        f"{sys.platform} cannot host this server",
        "Run the doctor where the server runs. On Windows that means inside WSL, "
        "through the same interpreter the MCP registration names.",
    )


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def check_workspace_roots(cfg: Config) -> Check:
    """Every configured root must exist and be a directory.

    `config.__post_init__` checks only that the tuple is non-empty -- there is no safe
    default for "which files may a model read", so an empty one is refused at load. It
    never asks whether the paths are there, and a root that is not resolves nothing: every
    prefetch and every `read_file` against it is refused, one call at a time.
    """
    missing: list[str] = []
    for root in paths.resolved_roots(cfg):
        if not Path(root).is_dir():
            missing.append(root)
    total = len(paths.resolved_roots(cfg))
    if not missing:
        return Check("workspace-roots", OK, f"{total} root(s), all present")
    return Check(
        "workspace-roots", FAIL,
        f"{len(missing)} of {total} root(s) missing: {', '.join(missing)}",
        "Fix DELEGATE_WORKSPACE_ROOTS, or create the directory. A missing root refuses "
        "every path under it, one call at a time, and never at startup.",
        {"missing": missing},
    )


def check_bwrap(cfg: Config) -> Check:
    """Does `bwrap` exist, and can it actually build a namespace?

    Two questions, because they fail apart. `sandbox.available` asks only whether the
    binary is on `PATH`; a kernel with user namespaces restricted, or a container without
    the capability, answers yes to that and refuses every `run_bash` afterwards. The probe
    uses the same mandatory symlinks `build_argv` emits -- without `usr/lib64` nothing
    dynamically linked runs and the error blames the executable rather than the missing
    loader (ADR-0021).
    """
    if not sandbox.available(cfg):
        return Check(
            "bwrap", FAIL, f"{cfg.bwrap_bin!r} is not on PATH",
            "Install bubblewrap. Without it run_bash is refused, though delegations that "
            "only read or write files still work.",
        )
    argv = [
        cfg.bwrap_bin, "--unshare-all", "--ro-bind", "/usr", "/usr",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
        "--", "/bin/true",
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_BWRAP_PROBE_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Check("bwrap", FAIL, f"{cfg.bwrap_bin} could not be run: {e}",
                     "See docs/TROUBLESHOOTING.md.")
    if proc.returncode == 0:
        return Check("bwrap", OK, f"{cfg.bwrap_bin} builds a namespace and runs a command")
    err = (proc.stderr or "").strip().splitlines()
    return Check(
        "bwrap", FAIL,
        f"{cfg.bwrap_bin} exited {proc.returncode}: {err[-1] if err else 'no message'}",
        "bubblewrap is installed but cannot unshare. Check that user namespaces are "
        "permitted on this kernel.",
    )


def check_limiter(cfg: Config) -> Check:
    """`prlimit` bounds a confined command; missing it is a narrower fault than no bwrap."""
    if sandbox.limiter_available(cfg):
        return Check("limiter", OK, "resource limits can be applied")
    return Check(
        "limiter", FAIL, "prlimit is not available, so a command cannot be bounded",
        "Install util-linux. This is separate from bubblewrap: one confines a command, "
        "the other bounds it.",
    )


def check_toolchain(cfg: Config) -> Check:
    """Is anything bound into the sandbox for a command to run?

    A `WARN` rather than a `FAIL`: a delegation that only reads files needs nothing here,
    and this server's read-heavy majority is exactly that. It is reported because the
    absence is invisible otherwise -- the setting's own help text calls an unresolved
    probe "the single most likely first-run sandbox failure", and it went undiagnosed for
    a session.
    """
    binds = sandbox.probe_toolchain_binds(cfg)
    if binds:
        source = "configured" if cfg.toolchain_binds else "found by probing for uv"
        return Check("toolchain", OK,
                     f"{len(binds)} read-only bind(s), {source}: {', '.join(binds)}",
                     extra={"binds": list(binds)})
    return Check(
        "toolchain", WARN,
        "nothing is bound, so a sandboxed command sees only what /usr provides",
        "Reading and writing files still work. To run a project's tests, provision an "
        "interpreter outside the workspace and name it in DELEGATE_TOOLCHAIN_BINDS -- "
        "inside the workspace the secret scan covers a virtualenv's own files.",
    )


def check_transcripts(cfg: Config) -> Check:
    """Is an operator record being written, and can the directory actually be created?"""
    if not transcript.enabled(cfg):
        return Check(
            "transcripts", WARN, "switched off, so nothing records what a delegation cost",
            "Set DELEGATE_TRANSCRIPT_DIR to make the viewer and the cost report work.",
        )
    try:
        directory = transcript.directory(cfg)
    except UntranslatablePath as e:
        return Check(
            "transcripts", FAIL, f"{cfg.transcript_dir!r} cannot name a file here: {e}",
            "A transcript failure is swallowed at runtime by design, so this is the only "
            "place it is reported. Name a directory this distribution can reach.",
        )
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = directory / ".doctor-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return Check(
            "transcripts", FAIL, f"{directory} is not writable: {e}",
            "A transcript failure is swallowed at runtime by design, so this is the only "
            "place it is reported.",
        )
    return Check("transcripts", OK, f"writable at {directory}")


def check_cross_process(cfg: Config) -> Check:
    """Are the admission rules counted per machine, or per connected client?

    `active: false` is not a failure -- it is how the server behaved before ADR-0040 and it
    still serves delegations. It is a `WARN` because the consequence is invisible from
    inside one process: every rule then bounds one editor window, and the cluster sees the
    configured limit multiplied by however many are open.
    """
    slots, reason = build_slots(cfg)
    if slots is not None:
        return Check("cross-process-slots", OK, "admission is counted machine-wide")
    return Check(
        "cross-process-slots", WARN,
        f"counted per process only: {reason}",
        "Each connected client is bounded separately, so real load on the cluster is "
        "higher than this process's numbers suggest.",
    )


def check_endpoints(cfg: Config, reg: registry.Registry) -> list[Check]:
    """Probe every registry entry, reusing exactly what `backend_status` reuses.

    `id_confirmed: false` beside a healthy endpoint is the case worth a separate verdict:
    the endpoint answers, and it is not serving the model the registry names, so a
    delegation to it would not do what it claims.
    """
    async def run_probes() -> list[dict[str, Any]]:
        cache = BackendCache(cfg)
        try:
            return [
                await probe_entry(cache, cfg, entry, is_default=entry.key == reg.default_key)
                for entry in reg.entries.values()
            ]
        finally:
            await cache.aclose()

    try:
        rows = asyncio.run(run_probes())
    except Exception as e:
        return [Check("endpoint", FAIL, f"probing failed: {e}",
                      "This is a bug rather than a configuration fault; the probe is "
                      "written never to raise.")]

    checks: list[Check] = []
    for row in rows:
        key = row.get("key", "?")
        status = row.get("status")
        confirmed = row.get("id_confirmed")
        detail = row.get("detail") or ""
        if status != STATUS_OK:
            checks.append(Check(
                f"endpoint:{key}", FAIL, f"{status}{': ' + detail if detail else ''}",
                "backend_status reports the same thing at runtime. Check models.toml and "
                "that the host resolves from inside WSL rather than only from Windows.",
            ))
        elif confirmed is False:
            checks.append(Check(
                f"endpoint:{key}", FAIL,
                "reachable, but not serving the model this entry names",
                "served_model_id must match what the endpoint reports. A delegation here "
                "would not do what the registry claims.",
            ))
        else:
            checks.append(Check(f"endpoint:{key}", OK, "reachable, serving the named model"))
    if not checks:
        checks.append(Check("endpoint", FAIL, "the registry has no entries",
                            "Add a model to models.toml."))
    return checks


def collect(cfg: Config, reg: registry.Registry) -> list[Check]:
    """Every check, in the order a reader should care about them."""
    return [
        check_platform(),
        check_workspace_roots(cfg),
        check_bwrap(cfg),
        check_limiter(cfg),
        check_toolchain(cfg),
        *check_endpoints(cfg, reg),
        check_transcripts(cfg),
        check_cross_process(cfg),
    ]


def worst(checks: list[Check]) -> str:
    return max((c.verdict for c in checks), key=lambda v: _RANK[v], default=OK)


def report(checks: list[Check], *, out=None) -> int:
    """Print the report and return the exit code: non-zero on any FAIL."""
    stream = out if out is not None else sys.stdout
    print("delegate-local doctor", file=stream)
    print(file=stream)
    for check in checks:
        print(_fmt(check), file=stream)
    print(file=stream)
    fails = sum(1 for c in checks if c.verdict == FAIL)
    warns = sum(1 for c in checks if c.verdict == WARN)
    if fails:
        print(f"FAIL: {fails} blocking, {warns} warning(s).", file=stream)
        return 1
    print(f"PASS ({warns} warning(s)).", file=stream)
    return 0


def main() -> int:
    """Load configuration the way the server does, then check the world around it."""
    # Platform first and alone: on Windows the rest cannot be answered, and config
    # loading would report a POSIX-shaped complaint about a machine that cannot host
    # the server at all.
    platform = check_platform()
    if platform.verdict == FAIL:
        return report([platform])

    try:
        cfg = config.load()
        reg = registry.load(cfg)
    except ConfigError as e:
        return report([
            platform,
            Check("configuration", FAIL, str(e),
                  "The server would refuse to start on this too, which is the one fault "
                  "it does report at startup."),
        ])

    return report(collect(cfg, reg))
