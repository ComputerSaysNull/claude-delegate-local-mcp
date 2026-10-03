"""Run one delegation from a shell, with no MCP server in the path.

`claude-delegate-local-mcp run` reaches here. It exists for a client-side limit: a
conversation speaking MCP holds one write-capable call in flight and releases the next at
`min(completion, 120s)`, so a six-wide fan-out costs 600s of stagger before any work is
shared, where six started this way span milliseconds (JOURNAL 2026-09-19).

The second reason is context. An MCP tool result lands in the caller's window whole; a
shell redirects this one into a file and the caller reads back only what it wants.

**stdout carries the result, so nothing may share it.** Safe for the reason `--doctor`
is: nothing speaks MCP to this command. It is also why `main.run` dispatches here
*before* building a server, which `tests/test_run_task.py` pins.

Every delegation still goes through `server.run_delegation`, so admission, the budget,
the transcript and every refusal behave as they do for the MCP tools. This module owns
argument handling and output shape, and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, replace
from typing import Any

_EFFORTS = ("off", "low", "high", "max", "inherit")


@dataclass(frozen=True, slots=True)
class _Gates:
    """The collaborators `run_delegation` takes but does not build."""

    windows: Any
    admission: Any
    rates: Any


def _parser() -> argparse.ArgumentParser:
    # argparse here, where the server avoids it: ADR-0027 keeps the transport and config
    # file settable in one place, and these options touch neither -- they are the
    # delegation's own arguments, which `config.load` does not own.
    p = argparse.ArgumentParser(
        prog="claude-delegate-local-mcp run",
        description="Run one delegation and print the result as JSON.",
    )
    p.add_argument("--task", help="The one question or instruction to send.")
    p.add_argument("--files", action="append", default=[], metavar="PATH",
                   help="Absolute path to prefetch. Repeatable.")
    p.add_argument("--effort", choices=_EFFORTS, default="low")
    p.add_argument("--model", default=None, help="A key from the model registry.")
    p.add_argument("--agent", default=None, help="An agent file to shape the work.")
    p.add_argument("--project", default=None, help="Where to look for the agent file.")
    p.add_argument("--workdir", default=None, help="Bound read-write for run_bash.")
    p.add_argument("--allowed-tool", action="append", default=None, metavar="NAME",
                   dest="allowed_tools", help="Narrow the toolset. Repeatable.")
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--max-turns", type=int, default=None)
    return p


def _load_config() -> Any:
    """`config.load` plus each configured root's own layer-2 additions (ADR-0116).

    The MCP tools get those from `session_scope`; `run` has no session, so it reads them
    here, once, for the roots as configured.
    """
    from . import config  # noqa: PLC0415
    from .paths import load_repo_ext_allowlists, resolved_roots  # noqa: PLC0415

    cfg = config.load()
    return replace(cfg, repo_ext_allowlists=load_repo_ext_allowlists(resolved_roots(cfg)))


def _build_pieces() -> tuple[Any, Any, Any, _Gates]:
    """Everything `run_delegation` receives rather than constructs.

    Imported inside the function, like the other subcommands: an ordinary server launch
    should not pay to import the delegation loop.
    """
    from . import registry  # noqa: PLC0415
    from .admission import Admission  # noqa: PLC0415
    from .loop import RateHistory  # noqa: PLC0415
    from .server import BackendCache, WindowCheck  # noqa: PLC0415
    from .slots import build_slots, rate_history_path  # noqa: PLC0415

    cfg = _load_config()
    reg = registry.load(cfg)
    slots, _ = build_slots(cfg)
    gates = _Gates(
        windows=WindowCheck(cfg),
        admission=Admission(cfg, slots),
        rates=RateHistory(
            path=rate_history_path(cfg),
            stamp=reg.resolve(None).served_model_id,
        ),
    )
    return cfg, reg, BackendCache(cfg), gates


async def _dispatch(*, cfg: Any, reg: Any, cache: Any, gates: _Gates, args: Any) -> dict:
    from .agents import load_agent  # noqa: PLC0415
    from .loop import RateSampler  # noqa: PLC0415
    from .server import run_delegation, sampling  # noqa: PLC0415

    agent = None
    if args.agent is not None:
        agent = load_agent(cfg, args.agent, args.project or args.workdir)
    # The ticker the server runs, for the same reason: a completed turn files no sample of
    # its own, so without this a `run` would read the durable memory and put nothing back
    # -- the cold start the memory exists to end, for every caller driving delegations
    # this way.
    sampler = RateSampler(
        gates.rates,
        probe=lambda: cache.get(reg.resolve(None)).probe_cluster(),
        busy=lambda: gates.admission.inflight_seqs > 0,
        every=cfg.rate_sample_seconds,
    )
    try:
        async with sampling(sampler):
            return await run_delegation(
                cfg, reg, cache, gates.windows, gates.admission,
                rates=gates.rates,
                task=args.task,
                files=list(args.files) or None,
                model=args.model,
                effort=args.effort,
                allowed_tools=args.allowed_tools,
                max_tokens=args.max_tokens,
                max_turns=args.max_turns,
                agent=agent,
                workdir=args.workdir,
                tool_name="run",
            )
    finally:
        await cache.aclose()


def main(*, argv: list[str] | None = None) -> int:
    """0 on a delegation that answered, 1 on one that did not, 2 on bad usage.

    A failure still prints JSON: a fan-out collects stdout per arm, and a bare traceback
    would replace one arm's result with something nothing can parse.
    """
    args = _parser().parse_args(sys.argv[2:] if argv is None else argv)
    if not args.task:
        print("run: --task is required", file=sys.stderr)
        return 2

    try:
        cfg, reg, cache, gates = _build_pieces()
        result = asyncio.run(
            _dispatch(cfg=cfg, reg=reg, cache=cache, gates=gates, args=args)
        )
    # Broad on purpose: every failure has to leave parseable JSON on stdout, and the
    # exception's class is the useful half of that report.
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}))
        return 1

    print(json.dumps(result, default=str))
    return 0
