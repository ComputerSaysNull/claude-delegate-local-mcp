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
    for bind in extra_binds:
        if not os.path.isabs(bind):
            raise AgentError(
                f"{where} extra_binds names {bind!r}, which is not an absolute path. A bind "
                "is resolved by the server, not by the delegated model's shell, so a "
                "relative path has nothing to be relative to."
            )

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
        network=(
            _coerce_bool(raw["network"], field="network", where=where)
            if "network" in raw
            else False
        ),
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


def list_agents(cfg: Config, workdir: str | None = None) -> tuple[AgentSpec, ...]:
    """Every agent visible from here, nearest tier first, shadowed ones dropped.

    A name found in more than one tier appears once, from the tier that would actually be
    used -- reporting both would describe a choice the lookup does not offer. A file that
    does not validate is skipped rather than fatal: one broken definition in a personal
    directory should not make the others impossible to discover.
    """
    seen: dict[str, AgentSpec] = {}
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
            if name in seen or not _NAME_RE.match(name):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                raw, body = parse_frontmatter(text, where=str(path))
                seen[name] = validate(cfg, raw, body, name=name, where=str(path))
            except (AgentError, OSError):
                continue
    return tuple(seen.values())
