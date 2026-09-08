r"""`--init`: write the two files a first run has no default for, from answers.

The install instructions have always been `cp .env.example .env` followed by "then set
DELEGATE_WORKSPACE_ROOTS", and `cp models.toml.example models.toml` followed by "then set
your endpoint". That is two edits to files the reader has not seen yet, and the failure
when either is skipped arrives much later, as a refusal one layer away from its cause --
the same gap `--doctor` exists to close from the other end. This closes it from the front:
ask, validate, write.

**A default is shown, not skipped, and not written.** Three tiers of question. A required
setting has no safe default and must be answered: `workspace_roots`, because there is no
defensible guess at which files a model may read, and a registry entry's `base_url` and
`served_model_id`, because nothing can invent an endpoint. An *offered* setting is asked
with its own default and its own help text displayed, and Enter accepts. Everything else
is not asked, and the written file points at the generated reference rather than
restating any of it.

The **writing** rule is the part that matters, and the reason is drift rather than
tidiness. An answer equal to the default emits no line. Writing today's
`DELEGATE_DISPATCH_TIMEOUT` into `.env` would freeze a copy of it: raise the default in
`config.py` next month and every generated file silently keeps the old value, with nothing
to compare it against. Config defaults live in `config.py` alone (CLAUDE.md), so this
module may read them and may print them, and must not write them down.

**Nothing here holds a default, a help string or a validation rule of its own.** The
prompts are built from `config.describe()` -- the same introspection
`scripts/gen_config_docs.py` renders the configuration reference from -- and from
`dataclasses.fields(registry.ModelEntry)`. What this module holds is a curated list of
field *names*, which is what `gen_config_docs.py` holds too. Validation is performed by
calling `registry._validate` and `config.load`, so a rule about a base URL or an effort
level cannot drift from the one the server enforces.

**Every path written into `.env` is in POSIX form**, translated as each answer is taken
rather than left as typed. `config.load` splits a tuple setting on `os.pathsep`, which is
`:` where the server runs, so a pasted drive letter cannot survive being stored verbatim --
and cannot survive being *parsed* out of a separated list either, which is why the roots
are asked one per line. `ask_roots` carries the measurement.

**An existing file is moved aside, never overwritten and never left in place.** Refusing
would leave a half-configured host with no route forward but hand-editing, which is the
state this command exists to remove. The backup is named in the output. Both backup names
are covered by `.gitignore` and by `security/secret_globs.txt`, which is load-bearing
rather than tidy: these files name a host, and an uncovered `models.toml.bak-*` would be an
ordinary untracked file one `git add -A` from being published.

**stdout is the conversation here, as it is for the doctor** and unlike every other entry
into this package: nothing speaks MCP to an `--init` run, so there is no protocol on stdout
to corrupt.

**No endpoint is probed.** `--doctor` already does that, reusing `server.probe_entry`, and
a second copy of the probe here would be a second thing to keep true. This command ends by
saying to run the doctor.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, registry
from .config import Config, ConfigError
from .registry import ModelEntry, RegistryError
from .wsl import UntranslatablePath, is_wsl, to_posix, to_windows

# The `.env` settings offered with their defaults. Names only -- every default, unit and
# help string is read from `config.describe()` at run time. The list is `.env.example`'s
# own "commonly adjusted" block plus `transcript_dir`, which is here because `--doctor`
# warns when it is unset and because it is a path, so it exercises the same
# pasted-Windows-path handling as the root list above it.
OFFERED_SETTINGS = (
    "transcript_dir",
    "thinking_default",
    "max_turns_default",
    "max_inflight_seqs",
    "dispatch_timeout",
)

# `models.toml` carries no metadata to read a prompt from, so a one-line label per field
# lives here. A label is prose, not a default: the values and which fields are mandatory
# still come from `ModelEntry` and `registry._REQUIRED`.
MODEL_LABELS = {
    "base_url": "Endpoint base URL, with no /v1 suffix -- the server appends the API path",
    "served_model_id": "Model id exactly as the endpoint reports it at /v1/models",
    "context_window": "Context window in tokens",
    "default_effort": f"Reasoning effort for this model, one of {config.EFFORT_LEVELS}",
    "max_tokens_cap": "Per-model ceiling on max_tokens, 0 for none",
    "concurrency": "This endpoint's own concurrent-sequence limit",
    "api_key_env": "Name of the env var holding a bearer token, blank when there is none",
}

# Asked in this order, after the two required ones.
OFFERED_MODEL_FIELDS = (
    "context_window", "default_effort", "max_tokens_cap", "concurrency", "api_key_env",
)

# Written for the single entry this command creates, though `registry.load` does not need
# it -- a lone unflagged entry is taken as the default. It is here for the second entry
# someone adds by hand later, which would otherwise turn a working registry into "N models
# and none marked default".
_DEFAULT_FLAG = "default = true"

# Fields whose value is written to TOML bare rather than quoted. Derived from the
# dataclass, so a field that changes type does not need remembering here. `bool` is
# excluded explicitly because it is a subclass of `int`.
_NUMERIC_MODEL_FIELDS = frozenset(
    f.name for f in fields(ModelEntry)
    if isinstance(f.default, int) and not isinstance(f.default, bool)
)


class Abandoned(Exception):
    """Input ended before the answers were complete. Not a fault -- nobody is there."""


@dataclass
class Answers:
    """What the prompts collected. Only departures from a default are recorded."""

    settings: dict[str, str] = field(default_factory=dict)   # field name -> raw answer
    model_key: str = ""
    model: dict[str, str] = field(default_factory=dict)      # field name -> raw answer


# --------------------------------------------------------------------------------------
# Prompting


def _read(inp, out) -> str:
    line = inp.readline()
    if line == "":
        # EOF, which is not an empty answer: an empty answer is a newline and means
        # "accept the default", while this means there is nobody to accept anything.
        print(file=out)
        raise Abandoned
    return line.strip()


def ask(label: str, *, default: str, inp, out,
        check: Callable[[str], str | None] | None = None) -> str:
    """One question. Returns the answer, or `default` when Enter was pressed.

    Re-asks on a rejected answer rather than failing the run: the answers already given
    are worth more than the one that was mistyped. The loop is bounded by EOF.
    """
    print(label, file=out)
    shown = f"[{default}] " if default != "" else ""
    while True:
        print(f"> {shown}", end="", file=out, flush=True)
        raw = _read(inp, out)
        value = raw if raw != "" else default
        if check is not None and (problem := check(value)) is not None:
            print(f"  {problem}", file=out)
            continue
        return value


def _nonempty(value: str) -> str | None:
    return None if value.strip() else "That one has no default and cannot be blank."


def _translatable(value: str) -> str | None:
    """Accept a pasted Windows path; refuse one with nowhere to land.

    Only the *translation* is checked. A directory that does not exist yet is reported and
    accepted: `--doctor` fails on a missing root, and someone naming a directory they are
    about to create is not making a mistake this command can be sure of.
    """
    if not value.strip():
        return None
    try:
        to_posix(value)
    except UntranslatablePath as e:
        return str(e)
    return None


def _is_path_setting(name: str) -> bool:
    return name.endswith(("_dir", "_roots"))


def _integer(value: str) -> str | None:
    try:
        int(value.strip())
    except ValueError:
        return f"{value!r} is not a whole number."
    return None


# --------------------------------------------------------------------------------------
# The interview


def _rows() -> dict[str, dict[str, Any]]:
    """`config.describe()` keyed by field name. Introspection, not a second list."""
    return {row["field"]: row for row in config.describe()}


def _setting_label(row: dict[str, Any]) -> str:
    unit = f" ({row['unit']})" if row["unit"] else ""
    return f"\n{row['env']}{unit}\n  {row['description']}"


def _model_label(name: str) -> str:
    return f"\n{name}\n  {MODEL_LABELS[name]}"


def ask_roots(row: dict[str, Any], *, inp, out) -> list[str]:
    r"""One root per question, until a blank line. Returns them translated.

    Asked one at a time rather than as a separated list, and the reason is measured rather
    than stylistic. A list would have to be split on `os.pathsep`, which is `:` here -- so
    `C:\Users\you\projects`, the form this command exists to accept, splits into `C` and
    `\Users\you\projects` *before* anything can translate it, and each half then survives
    `to_posix` untouched and rejoins into the original. The value round-trips and every
    path under it is refused, with nothing in the file looking wrong. Asking one at a time
    removes the parse instead of trying to be clever about it.

    Translated on the way in, for the same reason: what is written is what the loader will
    read, and `to_posix` is this project's one direction of travel.
    """
    print(f"\n{row['env']}\n  {row['description']}", file=out)
    print("  One per line. A pasted Windows path is accepted; blank line when done.",
          file=out)
    roots: list[str] = []
    while True:
        label = "" if not roots else f"  {len(roots)} so far."
        answer = ask(label, default="", inp=inp, out=out,
                     check=lambda v: (_nonempty(v) if not roots else None) or _translatable(v))
        if answer == "":
            return roots
        root = to_posix(answer)
        if not Path(root).is_dir():
            print(f"  note: {root} does not exist yet, and --doctor will fail on it until "
                  f"it does.", file=out)
        roots.append(root)


def interview_settings(rows: dict[str, dict[str, Any]], answers: Answers, *,
                       inp, out) -> None:
    """The required root list, then each offered setting with its own default shown."""
    answers.settings["workspace_roots"] = os.pathsep.join(
        ask_roots(rows["workspace_roots"], inp=inp, out=out))

    print("\nThe rest have defaults. Press Enter to keep one.", file=out)
    for name in OFFERED_SETTINGS:
        row = rows[name]
        shown = "" if row["default"] in ("", None) else str(row["default"])
        check = _translatable if name.endswith("_dir") else (
            _integer if row["type"] == "int" else None)
        got = ask(_setting_label(row), default=shown, inp=inp, out=out, check=check)
        # The writing rule: a line is emitted only for a departure. An accepted default is
        # not a value this file may write down.
        if got != shown:
            answers.settings[name] = to_posix(got) if _is_path_setting(name) else got


def interview_model(answers: Answers, *, inp, out) -> None:
    """One registry entry, checked by the registry's own validator rather than by a copy."""
    defaults = {f.name: f.default for f in fields(ModelEntry)}
    print("\nOne model to start with. A second one is a second table in models.toml.",
          file=out)
    answers.model_key = ask("\nRegistry key -- the name you delegate to", default="",
                            inp=inp, out=out, check=_nonempty)
    key = answers.model_key

    # `registry._REQUIRED` rather than a list here: it is the single statement of which
    # fields a registry table cannot omit, and this package is where it lives.
    for name in registry._REQUIRED:
        answers.model[name] = ask(
            _model_label(name), default="", inp=inp, out=out,
            check=lambda v, n=name: _nonempty(v) or _field_problem(key, n, v))

    for name in OFFERED_MODEL_FIELDS:
        shown = "" if defaults[name] in ("", None) else str(defaults[name])
        got = ask(_model_label(name), default=shown, inp=inp, out=out,
                  check=lambda v, n=name: _field_problem(key, n, v))
        if got != shown:
            answers.model[name] = got


def _field_problem(key: str, name: str, value: str) -> str | None:
    """Ask the registry's own validator about one field, the rest left at default.

    Reuse rather than reimplementation, for the reason `doctor.py` calls the server's own
    helpers: the rule that a base URL carries no `/v1` suffix, and that an effort level has
    a translation, are enforced in one place, and a copy here would be free to agree with a
    registry that had changed underneath it. The other required field is filled with a
    placeholder so only `name`'s own rules can fire.
    """
    raw: dict[str, Any] = {"base_url": "http://placeholder", "served_model_id": "placeholder"}
    if name in _NUMERIC_MODEL_FIELDS:
        if (problem := _integer(value)) is not None:
            return problem
        raw[name] = int(value)
    else:
        raw[name] = value
    try:
        registry._validate(key, raw, Path("models.toml"))
    except RegistryError as e:
        return str(e)
    return None


# --------------------------------------------------------------------------------------
# Rendering


def render_env(answers: Answers, rows: dict[str, dict[str, Any]], *,
               models_file: str = "") -> str:
    """The `.env` text. Every setting line here is a departure from a default."""
    lines = [
        "# Written by `claude-delegate-local-mcp --init`. Adjust freely; it is gitignored.",
        "#",
        "# Only the settings you changed are here. Every other setting, its default and",
        "# what it does: docs/CONFIGURATION.md, generated from config.py, which is the only",
        "# place a default lives. A value copied here would freeze the default it copied.",
        "",
    ]
    lines += [f"{rows[name]['env']}={value}" for name, value in answers.settings.items()]
    if models_file:
        lines.append(f"{config.env_name('models_file')}={models_file}")
    kept = [rows[n]["env"] for n in OFFERED_SETTINGS if n not in answers.settings]
    if kept:
        # Named, not valued. Which questions were asked and waved through is worth
        # recording; what the answer was is a default and belongs only in config.py.
        lines += ["", "# Asked and left at the default:"]
        lines += [f"#   {name}" for name in kept]
    return "\n".join(lines) + "\n"


def _toml_value(name: str, raw: str) -> str:
    return raw.strip() if name in _NUMERIC_MODEL_FIELDS else json.dumps(raw)


def render_models(answers: Answers) -> str:
    """The `models.toml` text. Written by hand: it is one table of scalars."""
    lines = [
        "# Written by `claude-delegate-local-mcp --init`. Gitignored on purpose: it names",
        "# a host, and a hostname identifies your machine as surely as an address does.",
        "#",
        "# Only what you answered is here. Every other field falls back to the default in",
        "# registry.ModelEntry -- docs/MODELS.md has the format and the whole field list.",
        "",
        f"[models.{answers.model_key}]",
    ]
    for name in (*registry._REQUIRED, *OFFERED_MODEL_FIELDS):
        if name in answers.model:
            lines.append(f"{name} = {_toml_value(name, answers.model[name])}")
    lines.append(_DEFAULT_FLAG)
    return "\n".join(lines) + "\n"


def registration(repo_root: Path | str) -> str:
    """The MCP registration block, printed because nothing here can write it.

    `README.md` keeps a generic version, which a reader needs before they can run anything.
    This is the machine-specific one: the resolved console script, and under WSL the
    `wsl.exe` wrapper around it carrying the distribution this process is running in.
    """
    command = shutil.which("claude-delegate-local-mcp") or "claude-delegate-local-mcp"
    note = ""
    if not is_wsl():
        entry: dict[str, Any] = {"command": command, "timeout": 900000}
    else:
        try:
            cd = to_windows(str(repo_root))
        except UntranslatablePath:
            # The repository is inside the distribution rather than under a drive mount, so
            # there is no Windows form to print. Name the argument the reader has to supply
            # instead of printing one that cannot work.
            cd = "<the Windows path of this repository>"
        entry = {
            "command": "wsl.exe",
            "args": ["-d", os.environ.get("WSL_DISTRO_NAME", "YOUR-DISTRO"),
                     "--cd", cd, "-e", command],
            "timeout": 900000,
        }
        note = "\n--cd takes the Windows form of the path; a /mnt/c spelling is rejected.\n"
    block = json.dumps({"mcpServers": {"delegate-local": entry}}, indent=2)
    return f"\nRegister this with Claude Code:\n\n{block}\n{note}"


# --------------------------------------------------------------------------------------
# Writing


def check_answers(answers: Answers, rows: dict[str, dict[str, Any]], tmp: Path) -> Config:
    """Load what is about to be written, through the code the server loads it with.

    Rendering to a temporary pair first means a rejected set of answers costs nothing: the
    real files stay untouched until both loads have succeeded. `config.load` is handed an
    explicit environ so it cannot pick up a `.env` that already sits beside the repository,
    which would make this check pass on a value it is about to replace.
    """
    models_path = tmp / "models.toml"
    models_path.write_text(render_models(answers), encoding="utf-8")
    environ = {rows[name]["env"]: value for name, value in answers.settings.items()}
    environ[config.env_name("models_file")] = str(models_path)
    cfg = config.load(environ=environ)
    registry.load(cfg)
    return cfg


def back_up(path: Path, *, stamp: str) -> Path | None:
    """Move an existing file aside. Returns where it went, or None if there was nothing.

    The name carries a timestamp rather than a counter so a second run cannot land on the
    first backup, and both shapes are covered by `.gitignore` and
    `security/secret_globs.txt` -- these files name a host, so an uncovered backup would be
    a leak with one `git add -A` behind it.
    """
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.bak-{stamp}")
    if target.exists():
        raise OSError(f"{target} already exists, and a backup is never overwritten.")
    path.rename(target)
    return target


def write_files(answers: Answers, rows: dict[str, dict[str, Any]], root: Path, *,
                out, stamp: str) -> tuple[Path, Path]:
    """Back up whatever is there, then write both files. Returns the two paths."""
    env_path, models_path = root / ".env", root / "models.toml"
    for path in (env_path, models_path):
        if (moved := back_up(path, stamp=stamp)) is not None:
            print(f"  moved aside: {moved.name}", file=out)

    # `models_file` defaults to `./models.toml`, relative to the working directory. Both
    # files are written beside the repository root, where `config.load` discovers `.env`;
    # when the two differ the default would not find the registry, so the path is written
    # -- a departure from the default rather than a copy of one.
    #
    # Translated like every other path this file carries, which matters for the one case
    # that is easy to miss: `--init` run on Windows, writing configuration a server inside
    # WSL will read. A native `C:\...` there would name nothing on the side that reads it.
    models_setting = "" if Path.cwd().resolve() == root else to_posix(str(models_path))
    env_path.write_text(render_env(answers, rows, models_file=models_setting),
                        encoding="utf-8")
    models_path.write_text(render_models(answers), encoding="utf-8")
    for path in (env_path, models_path):
        # Same reasoning as the transcript directory's mode: the contents name a host.
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return env_path, models_path


def run(*, root: Path, inp, out, stamp: str, tmp: Path) -> int:
    """The whole command, with every boundary injected so a test can drive it."""
    print("delegate-local init", file=out)
    print("\nTwo files have no safe default and so cannot be written for you: which "
          "directories\na delegated model may read, and where your endpoint is.", file=out)

    rows = _rows()
    answers = Answers()
    interview_settings(rows, answers, inp=inp, out=out)
    interview_model(answers, inp=inp, out=out)

    try:
        check_answers(answers, rows, tmp)
    except ConfigError as e:
        print(f"\nNothing was written -- what you answered does not load:\n\n  {e}\n",
              file=out)
        return 1

    print(file=out)
    env_path, models_path = write_files(answers, rows, root, out=out, stamp=stamp)
    print(f"  wrote: {env_path}", file=out)
    print(f"  wrote: {models_path}", file=out)
    print(registration(root), file=out)
    print("Now run `claude-delegate-local-mcp --doctor`, which asks what this command "
          "cannot:\nwhether the roots exist, whether bwrap runs, and whether the endpoint "
          "is serving\nthe model you named.", file=out)
    return 0


def main(*, inp=None, out=None) -> int:
    """Refuse when there is nobody to answer, then run the interview."""
    stream = out if out is not None else sys.stdout
    if inp is None:
        if not sys.stdin.isatty():
            print("--init asks questions and stdin is not a terminal, so there are no "
                  "answers to\nread. Run it in a terminal, or copy .env.example and "
                  "models.toml.example by hand.", file=sys.stderr)
            return 2
        inp = sys.stdin

    with tempfile.TemporaryDirectory(prefix="delegate-init-") as tmp:
        try:
            return run(root=config.REPO_ROOT, inp=inp, out=stream,
                       stamp=datetime.now().strftime("%Y%m%d-%H%M%S"), tmp=Path(tmp))
        except Abandoned:
            print("Nothing was written: input ended before the answers were complete.",
                  file=stream)
            return 2
