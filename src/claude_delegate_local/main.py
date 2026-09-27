"""The console-script entrypoint: load configuration, build the server, run it.

`pyproject.toml` points `claude-delegate-local-mcp` here. Claude Code launches that
command and speaks MCP to it over stdio, the constraint shaping this module.

**stdout belongs to the protocol.** A traceback, a `print`, or a logging handler on its
default stream corrupts every message after it, and the only symptom is Claude Code
reporting a server that failed for no stated reason. So a startup failure goes to stderr
with a non-zero exit, the one thing a launcher can report; to read it, run the command by
hand (docs/TROUBLESHOOTING.md).

`--doctor`, `--init`, `--install-skills`, `provision` and `run` are not really exceptions:
nothing speaks MCP to them, so there is no protocol on stdout to corrupt. All return
before any of the below.
"""

from __future__ import annotations

import sys
from typing import Literal, cast

from . import config, registry, server


def run() -> None:
    # `--doctor` before anything else: it exists for the case where the rest of this
    # function would succeed and be wrong. Matched, not parsed: the server takes no
    # arguments, and a parser would be a second place to set the transport and the config
    # file, which only `config.load` may do (ADR-0027).
    if "--doctor" in sys.argv[1:]:
        # Imported here on purpose: the doctor pulls in the whole server module, and an
        # ordinary launch should not pay for a report it will never print.
        from . import doctor  # noqa: PLC0415

        raise SystemExit(doctor.main())

    # `--init` writes the files `config.load` below requires, so it returns before that
    # runs. Matched for the same reason; deferred because it reads no configuration and
    # pulls in none of the server.
    if "--init" in sys.argv[1:]:
        from . import init  # noqa: PLC0415

        raise SystemExit(init.main())

    # `--install-skills` reads no configuration either -- the destination is the working
    # directory and the content ships in this package -- so it returns before
    # `config.load`. Unlike `--init` it asks nothing, so it runs with stdin closed, from an
    # agent's shell.
    if "--install-skills" in sys.argv[1:]:
        from . import install_skills  # noqa: PLC0415

        raise SystemExit(install_skills.main())

    # `provision` reads an argument rather than testing for one, so it is matched on
    # position, `sys.argv[1]`: membership would make any launch whose arguments contained
    # the word build a virtualenv instead of a server. Still not argparse, for the reason
    # above; the module validates its own argument and prints its own usage.
    if sys.argv[1:2] == ["provision"]:
        from . import provision  # noqa: PLC0415

        raise SystemExit(provision.main(argv=sys.argv[2:]))

    # `run` returns here like the four above, and for a sharper reason: it prints the
    # result as JSON on stdout, which a server underneath would interleave with MCP frames.
    # On position like `provision`, or any delegation whose *task text* held the word would
    # be diverted.
    if sys.argv[1:2] == ["run"]:
        from . import run_task  # noqa: PLC0415

        raise SystemExit(run_task.main(argv=sys.argv[2:]))

    try:
        cfg = config.load()
        reg = registry.load(cfg)  # RegistryError subclasses ConfigError
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e

    mcp = server.build(cfg, reg)

    # show_banner=False: the banner is cosmetic on a server nobody watches start, and
    # drawing it calls PyPI for a version check -- an outbound request on every launch,
    # from a tool whose point is that inference stays on hardware you control.
    #
    # The validated value, not the literal "stdio", and no branch on it: anything else was
    # refused at load, and one copy of the value means one place to change if a transport
    # is added.
    mcp.run(transport=cast("Literal['stdio']", cfg.transport), show_banner=False)
