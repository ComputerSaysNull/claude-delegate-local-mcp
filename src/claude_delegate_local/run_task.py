"""Run one delegation from a shell, with no MCP server in the path.

`claude-delegate-local-mcp run` reaches here. It exists because of a client-side limit
rather than a server one: a conversation speaking MCP holds a single write-capable call
in flight and releases the next at `min(completion, 120s)`, so a six-wide fan-out costs
600s of stagger before any work is shared. Measured 2026-09-19, six delegations started
this way instead span 88ms (JOURNAL 2026-09-19).

The second reason is context rather than time. An MCP tool result lands in the caller's
window whole; a shell redirects this one into a file and the caller reads back only what
it wants.

**stdout carries the result, so nothing may share it.** That is safe here for exactly the
reason `--doctor` is safe: nothing speaks MCP to this command, so there is no protocol on
stdout to corrupt. It is also why `main.run` must dispatch here *before* building a
server, which `tests/test_run_task.py` pins.

Every delegation still goes through `server.run_delegation`, so admission, the budget,
the transcript and every refusal behave exactly as they do for the MCP tools. This module
owns argument handling and output shape, and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

_EFFORTS = ("off", "low", "high", "max", "inherit")


@dataclass(frozen=True, slots=True)
class _Gates:
    """The collaborators `run_delegation` takes but does not build."""

    windows: Any
    admission: Any
    rates: Any


def _parser() -> argparse.ArgumentParser:
    # argparse here where the server deliberately avoids it: ADR-0027 keeps the transport
    # and the config file settable in one place only, and none of these options touch
    # either. They are the delegation's own arguments, which `config.load` does not own.
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


def _build_pieces() -> tuple[Any, Any, Any, _Gates]:
    """Everything `run_delegation` receives rather than constructs.

    Imported inside the function for the reason the other subcommands are: an ordinary
    server launch should not pay to import the delegation loop.
    """
    from . import config, registry  # noqa: PLC0415
    from .admission import Admission  # noqa: PLC0415
    from .loop import RateHistory  # noqa: PLC0415
    from .server import BackendCache, WindowCheck  # noqa: PLC0415
    from .slots import build_slots, rate_history_path  # noqa: PLC0415

    cfg = config.load()
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
    from .server import run_delegation  # noqa: PLC0415

    agent = None
    if args.agent is not None:
        agent = load_agent(cfg, args.agent, args.project or args.workdir)
    try:
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

    The failure still prints JSON, because a fan-out collects stdout per arm and a bare
    traceback there is one arm's result replaced by something nothing can parse.
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
