"""The console-script entrypoint: load configuration, build the server, run it.

`pyproject.toml` points `claude-delegate-local-mcp` here. Claude Code launches that
command and speaks MCP to it over stdio, which is the constraint shaping this module.

**stdout belongs to the protocol.** A traceback, a `print`, or a logging handler left
on its default stream will corrupt every message that follows, and the only symptom is
Claude Code reporting a server that failed for no stated reason. So a startup failure
is written to stderr and the process exits non-zero, which is the one thing a launcher
can actually report. To read that message, run the command by hand in a terminal --
docs/TROUBLESHOOTING.md says so, because there is nowhere else it can be seen.
"""

from __future__ import annotations

import sys

from . import config, registry, server


def run() -> None:
    try:
        cfg = config.load()
        reg = registry.load(cfg)  # RegistryError subclasses ConfigError
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e

    mcp = server.build(cfg, reg)

    # show_banner=False for two reasons. The banner is cosmetic on a server nobody
    # watches start, and drawing it calls out to PyPI for a version check -- an
    # outbound request on every launch, from a tool whose whole point is that the
    # inference stays on hardware you control.
    #
    # The validated value rather than the literal "stdio", and no branch on it: anything
    # else was refused at load, so a second branch here could only be reached by a config
    # that cannot exist. Passing `cfg.transport` also keeps one copy of the value -- a
    # literal here would be a second place to change when a transport is finally added.
    mcp.run(transport=cfg.transport, show_banner=False)
