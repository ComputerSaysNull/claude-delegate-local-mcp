"""Agent files: where they are found, what their frontmatter means, and what it binds.

An agent is a markdown file, not a tool. That is the whole point of the design: there are
five MCP tools and there will stay five, so a new *kind* of delegated task -- review,
test-writing, migration -- is a file someone writes rather than a code change and a
release. (ADR-0005)

This module does two things and deliberately not a third. It **finds** a definition, by a
three-tier lookup that lets a project override a personal default, and it **validates**
one, refusing anything it does not understand rather than ignoring it. It does not
*resolve* precedence: an `AgentSpec` carries `model` as a string and never touches the
registry, because precedence is the caller's to apply and doing it here would put the
resolution in a second place. See `docs/AGENTS.md`.

The bug this format exists to avoid is worth naming, because it is easy to reintroduce and
it is invisible when it happens. In the ancestor, frontmatter was loaded and then largely
ignored -- `model:` did nothing. Everything here is shaped to make that failure loud: an
unknown key is refused rather than dropped, a misspelt `effort` is refused rather than
defaulted, and the resolved `ModelEntry` must be the same object for the concurrency bucket
and for the call, or a request is counted against one endpoint and sent to another.
(ADR-0031)

A note on the file format, since it looks like YAML and is not. This is a fixed, known set
of scalar, boolean, integer and short-list fields, so it is parsed by hand rather than by
adding a YAML dependency to a server whose entire runtime is `fastmcp` and `httpx`. What
that costs is real and is paid deliberately: nested maps, block scalars, anchors and
multi-line strings are refused, not guessed at. What it buys is that no file can mean
something subtly different from what it looks like.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import EFFORT_LEVELS, Config
from .loop import InvalidDelegation
from .paths import path_within_roots, resolved_agent_bind_roots
from .sandbox import base_mount_targets, resolve_home
from .tools import ALL_TOOL_NAMES


class AgentError(InvalidDelegation):
    """A bad agent name, a missing definition, or frontmatter that will not validate.

    Subclasses `InvalidDelegation` so `server.py` translates it to a `ToolError` through the
    branch it already has. A bad agent file is a caller's argument being wrong, in the same
    sense a bad `effort` is: it is settled before anything reaches a backend.
    """


# An agent name is not a path. Allowing it to look like one would make it a traversal, so
# the check runs before any filesystem access -- that is the property the test pins down,
# and it is the one that matters: an unvalidated name must never reach a `stat`.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Everything a frontmatter block may contain. The set is closed on purpose: an unknown key
# is refused, because the alternative is a typo costing a setting in silence, which is the
# ancestor bug wearing a different hat.
_SCALAR_FIELDS = frozenset({"name", "description", "model", "effort"})
_INT_FIELDS = frozenset({"max_turns", "max_tokens", "keep_tool_results"})
_BOOL_FIELDS = frozenset({"network"})
_LIST_FIELDS = frozenset({"allowed_tools", "extra_binds"})
KNOWN_FIELDS = _SCALAR_FIELDS | _INT_FIELDS | _BOOL_FIELDS | _LIST_FIELDS

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One validated agent definition: the frontmatter, plus the body that becomes a prompt.

    `None` and empty are different everywhere they can be. `allowed_tools=None` means the
    file did not say, so the caller's own default applies; `allowed_tools=()` means the file
    said *no tools*, which routes a delegation to the one-shot path instead of the turn
    loop. Collapsing the two would silently turn an agent that asked for nothing into an
    agent that gets everything.
    """

    name: str
    source_path: str
    description: str = ""
    model: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    max_tokens: int | None = None
    keep_tool_results: int | None = None
    allowed_tools: tuple[str, ...] | None = None
    network: bool = False
    extra_binds: tuple[str, ...] = ()
    body: str = ""


def _candidates(cfg: Config, name: str, workdir: str | None) -> list[Path]:
    """The three locations, in the order they are searched. Pure -- no I/O.

    Project-local beats personal, which is usually what someone wants: a repository can ship
    a `test-writer` that knows its own conventions without anyone editing their home
    directory. Split out from `find_agent_file` so the not-found message and the search use
    one list rather than two that can disagree.
    """
    found: list[Path] = []
    if workdir:
        root = Path(workdir)
        found.append(root / ".claude" / "agents" / f"{name}.md")
        found.append(root / ".claude" / "skills" / name / "SKILL.md")
    found.append(Path(os.path.expanduser(cfg.agents_dir)) / f"{name}.md")
    return found


def find_agent_file(cfg: Config, name: str, workdir: str | None = None) -> Path:
    """Three tiers, first match wins. Validates `name` before touching the filesystem."""
    if not _NAME_RE.match(name):
        raise AgentError(
            f"agent_name={name!r} is not a valid agent name: it must match "
            f"{_NAME_RE.pattern} -- letters, digits, '_' and '-' only. An agent name is a "
            "name, not a path, and one that looked like a path would be a traversal."
        )
    tried = _candidates(cfg, name, workdir)
    for candidate in tried:
        if candidate.is_file():
            return candidate
    searched = "\n  ".join(str(p) for p in tried)
    hint = (
        ""
        if workdir
        else " No workdir was given, so only the personal directory could be searched."
    )
    raise AgentError(
        f"No agent named {name!r} exists. Searched, in order:\n  {searched}\n"
        f"Create one of these, or check the spelling.{hint}"
    )


def _scalar(raw: str) -> str:
    """One frontmatter value, unquoted. Quotes are optional and stripped when balanced."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str, *, where: str) -> tuple[dict[str, str], str]:
    """Split the leading `---` block from the body. Returns raw string values and the body.

    Values stay strings here; typing them is `_coerce`'s job, so a type error can name the
    field and the file rather than surfacing as a `ValueError` from somewhere in the middle.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise AgentError(
            f"{where} has no frontmatter. An agent file opens with a '---' line, closes the "
            "block with another, and everything after it is the prompt."
        )
    fields: dict[str, str] = {}
    for lineno, line in enumerate(match.group(1).splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            raise AgentError(
                f"{where}:{lineno} is indented. This format is flat -- one 'key: value' per "
                "line, with no nested blocks, and it refuses what it cannot read rather "
                "than guessing at it."
            )
        key, sep, value = line.partition(":")
        if not sep:
            raise AgentError(
                f"{where}:{lineno} is not a 'key: value' line: {line.strip()!r}."
            )
        key = key.strip()
        if key in fields:
            raise AgentError(
                f"{where}:{lineno} sets {key!r} twice. Which one wins is not something this "
                "format decides for you."
            )
        fields[key] = value
    return fields, text[match.end():]


def _coerce_list(value: str, *, field: str, where: str) -> tuple[str, ...]:
    """`[a, b]`, or `[]`. A flow sequence and nothing else -- block lists are refused."""
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise AgentError(
            f"{where} {field}={text!r} is not a list. Write it inline, as "
            f"{field}: [one, two], or as {field}: [] for none. A '-' block list is not read "
            "by this format."
        )
    inner = text[1:-1].strip()
    if not inner:
        return ()
    return tuple(_scalar(part) for part in inner.split(",") if part.strip())


def _coerce_int(value: str, *, field: str, where: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise AgentError(
            f"{where} {field}={value.strip()!r} is not a whole number."
        ) from None


def _coerce_bool(value: str, *, field: str, where: str) -> bool:
    text = value.strip().lower()
    if text in ("true", "yes", "on"):
        return True
    if text in ("false", "no", "off"):
        return False
    raise AgentError(
        f"{where} {field}={value.strip()!r} is not a boolean. Write true or false."
    )


def _check_binds(cfg: Config, binds: tuple[str, ...], *, where: str) -> tuple[str, ...]:
    """Every `extra_binds` rule, returning the resolved paths the sandbox will mount.

    Separate from `validate` because it is the only field with four rules rather than
    one, and folding them inline pushed that function past what anyone can read at once.
    """
    roots = resolved_agent_bind_roots(cfg)
    # HOME joins the base mounts here because it is bound before `extra_binds` too, and so
    # is shadowable in exactly the same way -- but it is a config value rather than part of
    # the static table, so `base_mount_targets` cannot know about it.
    reserved = base_mount_targets() | {resolve_home(cfg)}
    resolved: list[str] = []
    for bind in binds:
        if not os.path.isabs(bind):
            raise AgentError(
                f"{where} extra_binds names {bind!r}, which is not an absolute path. A bind "
                "is resolved by the server, not by the delegated model's shell, so a "
                "relative path has nothing to be relative to."
            )
        # Resolved once, here, and it is the resolved value that is carried forward -- the
        # same shape as `resolve_workdir`. Checking one path and mounting another is what
        # makes a containment check decorative, since bwrap resolves the name it is given
        # at mount time and a link inside a root can point anywhere. Redirection is what
        # this closes; substitution -- a different directory later moved to an approved
        # path -- is a function of the path either way and no layer here can see it.
        real = os.path.realpath(bind)
        shadowed = sorted(t for t in reserved if path_within_roots(t, (real,)))
        if shadowed:
            raise AgentError(
                f"{where} extra_binds names {bind!r}, which sits at or above "
                f"{', '.join(shadowed)} -- mounts the sandbox makes for itself. Bound after "
                "those, it would replace them with a tree of this file's choosing. "
                "Read-only, so what it takes is trust rather than write access: a "
                "/usr/bin/python3 the sandbox did not provide. A bind *inside* one of them "
                "is fine and is how a toolchain under /tmp works."
            )
        if not path_within_roots(real, roots):
            raise AgentError(
                f"{where} extra_binds names {bind!r}, whose real location {real} is outside "
                "every configured agent bind root. An agent file is not an operator "
                "decision -- a repository can ship one, and this one grants a read-only "
                "mount of a host path -- so its binds are checked against "
                "DELEGATE_AGENT_BIND_ROOTS rather than trusted for being absolute. "
                f"Configured roots: "
                f"{', '.join(roots) or '(none, so no agent bind is permitted at all)'}."
            )
        resolved.append(real)
    return tuple(resolved)


def _check_network_grant(cfg: Config, *, name: str, where: str) -> None:
    """Refuse `network: true` unless the operator named this agent *and* wrote the file.

    Two conditions, and the second is not decoration. `--share-net` has no destination
    list, no proxy and no filter: granting it hands the delegated model everything this
    workstation can reach, so unlike a bind there is no root to pin it to and the grant is
    unbounded once given. That asymmetry is why the two fields are gated differently, and
    why copying one rule onto the other would be a downgrade in one direction and pointless
    in the other.

    A name alone is not enough because `_candidates` searches the workspace tiers *first*:
    a repository shipping `.claude/agents/<an-allowlisted-name>.md` would shadow the
    operator's own file and inherit its grant by matching a string an attacker picked. The
    provenance test is what that string cannot forge.
    """
    if name not in cfg.agent_network_allowed:
        raise AgentError(
            f"{where} asks for network: true, but {name!r} is not in "
            "DELEGATE_AGENT_NETWORK_ALLOWED. Egress is all-or-nothing here -- there is no "
            "destination list to narrow it with -- so it is refused by default rather than "
            "dropped, because an agent that quietly ran without the network it asked for "
            "would fail somewhere else entirely."
        )
    # `Path.is_relative_to` rather than the POSIX containment `extra_binds` uses. A bind is
    # a sandbox-side path and always POSIX; an agent *file* is read by this process, which
    # on Windows means a native path with native separators, and comparing those with "/"
    # rules would silently answer no to everything.
    personal = Path(os.path.realpath(os.path.expanduser(cfg.agents_dir)))
    if not Path(os.path.realpath(where)).is_relative_to(personal):
        raise AgentError(
            f"{where} asks for network: true from outside {personal}. Naming an agent in "
            "DELEGATE_AGENT_NETWORK_ALLOWED grants the network to the file you wrote "
            "there, not to any file that claims the name: a repository can ship one under "
            "the same name and it is searched first. Move the file, or drop network: true."
        )


def validate(
    cfg: Config, raw: dict[str, str], body: str, *, name: str, where: str
) -> AgentSpec:
    """Every frontmatter rule, in one place, refusing rather than defaulting.

    Ordered so the cheapest and most likely mistake -- a misspelt key -- is reported first.
    A file that fails any check produces no `AgentSpec` at all: a partially applied agent
    definition is worse than none, because it runs and looks like it worked.
    """
    unknown = set(raw) - KNOWN_FIELDS
    if unknown:
        raise AgentError(
            f"{where} has unknown frontmatter key(s) {sorted(unknown)}. Known keys are "
            f"{sorted(KNOWN_FIELDS)}. Refusing rather than ignoring them: a typo would "
            "otherwise cost you the setting in silence, and a setting that silently does "
            "nothing is the bug this format was rewritten to prevent."
        )

    declared = _scalar(raw.get("name", ""))
    if declared and declared != name:
        raise AgentError(
            f"{where} declares name={declared!r} but was found as {name!r}. The filename is "
            "what callers use, so a disagreeing 'name:' means one of the two is a lie. "
            "Rename the file or fix the field."
        )

    effort = _scalar(raw["effort"]) if "effort" in raw else None
    if effort is not None and effort not in EFFORT_LEVELS:
        extra = (
            " Claude Code's own agent format accepts 'medium'; this one deliberately does "
            "not, because the backend has no such level to translate it into (ADR-0031)."
            if effort == "medium"
            else ""
        )
        raise AgentError(
            f"{where} effort={effort!r} is not one of {EFFORT_LEVELS}.{extra}"
        )

    allowed: tuple[str, ...] | None = None
    if "allowed_tools" in raw:
        allowed = _coerce_list(raw["allowed_tools"], field="allowed_tools", where=where)
        unimplemented = sorted(set(allowed) - ALL_TOOL_NAMES)
        if unimplemented:
            raise AgentError(
                f"{where} allowed_tools names {unimplemented}, which this server does not "
                f"implement. Available: {sorted(ALL_TOOL_NAMES)}. Note that a tool present "
                "here may still be withheld at run time on a host that cannot run it."
            )

    max_turns = (
        _coerce_int(raw["max_turns"], field="max_turns", where=where)
        if "max_turns" in raw
        else None
    )
    if max_turns is not None:
        if max_turns < 1:
            raise AgentError(
                f"{where} max_turns={max_turns} must be at least 1. A delegation with no "
                "turns cannot produce an answer."
            )
        if max_turns > cfg.max_turns_hard_cap:
            # Deliberately unlike `resolve_max_turns`, which clamps a *caller's* number in
            # silence because the work is legitimate and only the number is not. A call
            # argument is transient; a file is committed, read again, and trusted. Clamping
            # here would leave a wrong number sitting in it indefinitely, appearing to work.
            raise AgentError(
                f"{where} max_turns={max_turns} exceeds DELEGATE_MAX_TURNS_HARD_CAP "
                f"({cfg.max_turns_hard_cap}). Lower it in the file. A caller passing this "
                "number would be clamped quietly, but a file is read again by whoever "
                "maintains it, so a wrong number here is told rather than hidden."
            )

    max_tokens = (
        _coerce_int(raw["max_tokens"], field="max_tokens", where=where)
        if "max_tokens" in raw
        else None
    )
    if max_tokens is not None and max_tokens < 1:
        raise AgentError(
            f"{where} max_tokens={max_tokens} must be at least 1. It is a reply budget, and "
            "the model's own cap is applied to it afterwards regardless."
        )

    keep = (
        _coerce_int(raw["keep_tool_results"], field="keep_tool_results", where=where)
        if "keep_tool_results" in raw
        else None
    )
    if keep is not None and keep < 0:
        raise AgentError(
            f"{where} keep_tool_results={keep} cannot be negative. Zero means every tool "
            "result is evicted as soon as the next turn starts."
        )

    extra_binds = (
        _coerce_list(raw["extra_binds"], field="extra_binds", where=where)
        if "extra_binds" in raw
        else ()
    )
    extra_binds = _check_binds(cfg, extra_binds, where=where)

    network = (
        _coerce_bool(raw["network"], field="network", where=where)
        if "network" in raw
        else False
    )
    if network:
        _check_network_grant(cfg, name=name, where=where)

    return AgentSpec(
        name=name,
        source_path=where,
        description=_scalar(raw.get("description", "")),
        model=_scalar(raw["model"]) if "model" in raw else None,
        effort=effort,
        max_turns=max_turns,
        max_tokens=max_tokens,
        keep_tool_results=keep,
        allowed_tools=allowed,
        network=network,
        extra_binds=extra_binds,
        body=body.strip(),
    )


def load_agent(cfg: Config, name: str, workdir: str | None = None) -> AgentSpec:
    """Find, parse and validate one agent. The only entry point a caller needs."""
    path = find_agent_file(cfg, name, workdir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise AgentError(f"{path} could not be read: {e}") from e
    raw, body = parse_frontmatter(text, where=str(path))
    return validate(cfg, raw, body, name=name, where=str(path))


# Frontmatter keys that belong to Claude Code's agent format and never to this one. A file
# carrying one is *that* format's file rather than a malformed version of this one, and
# ADR-0031 is explicit that the two are not portable -- Claude Code spells the tool list
# `tools` where this format spells it `allowed_tools`. Keeping the two answers apart is not
# tidiness: four of this repository's own five agent files carry `tools`, deliberately, so
# folding them into "broken" would leave that list permanently non-empty here and therefore
# unread -- the same reason `scan-coverage` stays silent about binary files.
FOREIGN_KEYS = frozenset({"tools"})


@dataclass(frozen=True, slots=True)
class SkippedAgent:
    """A file that meant to be an agent for this server and could not be read as one.

    `name` is what the filename claimed, so it is the name a caller would have typed --
    which is the whole point. The old advice for a missing agent was to ask for it by name
    with `delegate_to_agent` and read the error; that needs the name, and an omission is
    exactly what hides it.

    Non-empty means something needs fixing. That is the property worth protecting, and it
    is why a file in the other format is reported as `ForeignAgent` instead.
    """

    name: str
    source_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ForeignAgent:
    """A file in Claude Code's agent format, which this server is not meant to load.

    Reported rather than hidden, because "this exists but belongs to the other reader" is
    a third answer and a useful one: it tells a caller the name is taken and where to look,
    without claiming anything is wrong.
    """

    name: str
    source_path: str
    foreign_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentListing:
    """What one walk of the agent directories found, in three categories.

    A named result rather than a wider tuple: three parallel sequences at a call site is
    the shape where the caller eventually unpacks them in the wrong order.
    """

    agents: tuple[AgentSpec, ...]
    skipped: tuple[SkippedAgent, ...]
    other_format: tuple[ForeignAgent, ...]


def survey_agents(cfg: Config, workdir: str | None = None) -> AgentListing:
    """Every visible agent, everything broken, and everything in the other format.

    Skipping a broken definition is still right -- one bad file in a personal directory
    must not make the others undiscoverable -- but skipping it *silently* made "no such
    agent" and "that agent is broken" the same answer.

    Two things are deliberately not skips. A name shadowed by a nearer tier is not broken:
    the lookup really does offer only one, so reporting the loser would describe a choice
    that does not exist. Nor is a filename that could never be an agent name -- `_NAME_RE`
    governs what may be typed, and a file nobody could address was never a candidate.

    A file in Claude Code's format is its own category. A `tools` key is not a typo for
    `allowed_tools`; it is a different format's file sitting in a shared directory, which
    ADR-0031 says is expected and CONTRIBUTING.md records as temporary.
    """
    seen: dict[str, AgentSpec] = {}
    skipped: dict[str, SkippedAgent] = {}
    foreign: dict[str, ForeignAgent] = {}
    directories: list[tuple[Path, str]] = []
    if workdir:
        directories.append((Path(workdir) / ".claude" / "agents", "*.md"))
        directories.append((Path(workdir) / ".claude" / "skills", "*/SKILL.md"))
    directories.append((Path(os.path.expanduser(cfg.agents_dir)), "*.md"))

    for directory, pattern in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(pattern)):
            name = path.parent.name if path.name == "SKILL.md" else path.stem
            if name in seen or name in skipped or name in foreign:
                continue
            if not _NAME_RE.match(name):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                raw, body = parse_frontmatter(text, where=str(path))
            except OSError as e:
                # Reported, and separately worth having: an unreadable file is a
                # permissions or encoding problem rather than a malformed definition, and
                # the two are fixed differently.
                skipped[name] = SkippedAgent(
                    name, str(path), f"it could not be read: {e.strerror or e}"
                )
                continue
            except AgentError as e:
                skipped[name] = SkippedAgent(name, str(path), str(e))
                continue

            # Checked before validation, because validation would refuse the foreign key
            # as an unknown one and report the file as broken -- which is the conflation
            # this category exists to undo.
            claimed = FOREIGN_KEYS & set(raw)
            if claimed:
                foreign[name] = ForeignAgent(name, str(path), tuple(sorted(claimed)))
                continue

            try:
                seen[name] = validate(cfg, raw, body, name=name, where=str(path))
            except AgentError as e:
                skipped[name] = SkippedAgent(name, str(path), str(e))

    return AgentListing(
        tuple(seen.values()), tuple(skipped.values()), tuple(foreign.values())
    )


def list_agents(cfg: Config, workdir: str | None = None) -> tuple[AgentSpec, ...]:
    """Every agent visible from here, nearest tier first, shadowed ones dropped.

    A name found in more than one tier appears once, from the tier that would actually be
    used -- reporting both would describe a choice the lookup does not offer.

    The specs alone, for callers that resolve an agent rather than report on the
    directory. `survey_agents` is the same walk and also reports what it could not read and
    what belongs to the other format.
    """
    return survey_agents(cfg, workdir).agents
