"""The console-script entrypoint: load configuration, build the server, run it.

`pyproject.toml` points `claude-delegate-local-mcp` here. Claude Code launches that
command and speaks MCP to it over stdio, which is the constraint shaping this module.

**stdout belongs to the protocol.** A traceback, a `print`, or a logging handler left
on its default stream will corrupt every message that follows, and the only symptom is
Claude Code reporting a server that failed for no stated reason. So a startup failure
is written to stderr and the process exits non-zero, which is the one thing a launcher
can actually report. To read that message, run the command by hand in a terminal --
docs/TROUBLESHOOTING.md says so, because there is nowhere else it can be seen.

`--doctor`, `--init` and `provision` are the exceptions, and they are not really
exceptions: nothing speaks MCP to any of them, so there is no protocol on stdout for them
to corrupt. All three return before any of the below.
"""

from __future__ import annotations

import sys

from . import config, registry, server


def run() -> None:
    # `--doctor` before anything else, because it exists for the case where the rest of
    # this function would succeed and be wrong. It is matched rather than parsed: the
    # server takes no arguments at all, so an argument parser here would be a second
    # place for the transport and the config file to be settable, and `config.load` is
    # the only sanctioned route to either (ADR-0027).
    if "--doctor" in sys.argv[1:]:
        # Imported here rather than at the top on purpose: the doctor pulls in the whole
        # server module, and an ordinary launch should not pay for a report it will never
        # print. The suppression below is for that, not an oversight.
        from . import doctor  # noqa: PLC0415

        raise SystemExit(doctor.main())

    # `--init` writes the files `config.load` below is about to require, so it too has to
    # return before that runs. Matched rather than parsed for the same reason as above, and
    # deferred for a narrower one: it reads no configuration at all and pulls in none of
    # the server.
    if "--init" in sys.argv[1:]:
        from . import init  # noqa: PLC0415

        raise SystemExit(init.main())

    # `--install-skills` reads no configuration either -- the destination is the working
    # directory and the content ships in this package -- so it returns before `config.load`
    # for the same reason `--init` does. Unlike `--init` it asks nothing, which is what lets
    # it run with stdin closed, from an agent's shell rather than a terminal.
    if "--install-skills" in sys.argv[1:]:
        from . import install_skills  # noqa: PLC0415

        raise SystemExit(install_skills.main())

    # `provision` is the first command here that reads an argument rather than testing for
    # one, so it is matched on position: `sys.argv[1]`, not membership. Membership would
    # make any delegation whose *project path* happened to contain the word provision start
    # building a virtualenv instead of a server. Still not argparse, for the reason above --
    # the module validates its own one argument and prints its own usage.
    if sys.argv[1:2] == ["provision"]:
        from . import provision  # noqa: PLC0415

        raise SystemExit(provision.main(argv=sys.argv[2:]))

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
