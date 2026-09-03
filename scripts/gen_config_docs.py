#!/usr/bin/env python
"""Generate the configuration reference from the code that defines it.

`docs/CONFIGURATION.md` is not written by hand. It is rendered from the field metadata
on `Config`, between the GEN markers, and the docs gate fails if the committed file
differs from what this script produces.

That is the whole anti-drift mechanism for config facts (ADR-0004). The ancestor project
documents one setting as three different values in three places; this makes that
impossible rather than merely discouraged.

Usage:
    python scripts/gen_config_docs.py           # write
    python scripts/gen_config_docs.py --check   # exit 1 if stale, print a diff
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
import tempfile
from pathlib import Path

# Read the real source, never a cached compile. Python validates a .pyc on
# (mtime, size), so an edit that changes a default WITHOUT changing the file's byte
# length -- 6 to 9, true to True, one flag to another -- can be missed if it lands
# within a single filesystem timestamp tick. This script would then happily report the
# doc as current against code that no longer exists, which is the exact failure it is
# supposed to catch. Observed while building this, not hypothetical.
#
# `-B` / sys.dont_write_bytecode is NOT sufficient: it prevents WRITING a cache, not
# READING an existing stale one. Redirecting the cache to an empty temp directory is
# what actually forces a fresh compile.
sys.pycache_prefix = tempfile.mkdtemp(prefix="cdl-gen-pyc-")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_delegate_local import config  # noqa: E402

TARGET = ROOT / "docs" / "CONFIGURATION.md"
START = "<!-- GEN:CONFIG:START -->"
END = "<!-- GEN:CONFIG:END -->"

# Field-name prefixes grouped into sections, in render order. A field matching no
# group lands in "Other" -- which is a visible prompt to place it, not a silent bucket.
SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Backend selection", ("models_file", "default_model")),
    ("Path policy", ("workspace_roots", "workdir_roots", "ext_allowlist",
                     "secret_globs_file", "respect_gitignore")),
    ("Context prefetch", ("max_file_tokens", "max_total_prefetch_tokens",
                          "max_file_read_bytes")),
    ("Model-facing tool limits", ("max_read_chars", "max_write_bytes",
                                  "search_max_files_scanned",
                                  "run_bash_timeout")),
    ("Generation budgets", ("max_tokens", "thinking_default",
                            "thinking_max_tokens_floor", "resend_reasoning",
                            "tool_call_temperature", "one_shot_temperature")),
    ("Agentic loop", ("max_turns_default", "max_turns_hard_cap",
                      "keep_tool_results", "max_batch_size")),
    ("Context overflow", ("context_overflow_enabled", "overflow_plateau_slop_tokens",
                          "overflow_min_growth_tokens", "overflow_reserve_fraction",
                          "overflow_tightened_keep_tool_results", "overflow_probe_cache_ttl")),
    ("Timeouts and retries", ("turn_timeout", "connect_timeout", "dispatch_timeout",
                              "stall_timeout",
                              "status_probe_timeout", "keepalive_interval",
                              "retry_max_attempts",
                              "retry_base_delay", "retry_max_delay")),
    ("Admission control", ("max_inflight_seqs", "kv_token_budget",
                           "large_prefill_tokens", "max_inflight_large_prefills",
                           "admission_wait_timeout", "cross_process_slots",
                           "slots_dir")),
    ("Operator transcript", ("transcript_dir",)),
    ("Sandbox", ("bwrap_bin", "sandbox_home", "toolchain_binds", "env_passthrough",
                 "max_bash_output_chars",
                 "secret_shadow_max_entries", "secret_shadow_max_depth",
                 "opaque_globs_file")),
    ("Agents", ("agents_dir",)),
    ("Transport", ("transport", "http_port")),
]


def _code_identifiers(source: str) -> set[str]:
    """Every identifier `source` actually references, ignoring comments and literals.

    Prose is not a use. A comment naming a field -- including one truthfully saying the
    field is unused -- must not count, or the marker inverts: the sentence documenting a
    setting as dead is what reports it live.

    Parsed rather than tokenised, and that is not a preference. Tokenising and keeping NAME
    tokens gives the right answer only from Python 3.12, where PEP 701 split f-strings into
    their parts; before that an f-string is a single STRING token, so `f"{cfg.some_field}"`
    is a real read that reads as prose. Since this feeds a *generated* file, that would make
    the rendered document depend on which interpreter rendered it. The syntax tree has an
    expression node for the substitution on every supported version.

    Attribute names are collected as well as bare names, because a setting is nearly always
    reached as `cfg.<field>` and `<field>` is the attribute half. A name reached only
    through a string -- `getattr(cfg, "some_field")` -- is still unread here, which is the
    conservative direction `_unread_fields` documents and keeps deliberately.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _unread_fields() -> set[str]:
    """Field names no module outside config.py reads.

    Computed, never hand-maintained. Eighteen settings for unbuilt subsystems -- the
    sandbox, the model-facing tools, admission control -- were rendered identically to
    settings that work, so the reference read as a list of live knobs when a third of it
    was inert (second audit, 2026-08-27). A hand-kept list of "not built yet" would be a
    second copy of PLAN.md and would rot the day a subsystem landed; a scan of the source
    clears itself in the same commit that starts using the setting.

    Deliberately conservative: a plain identifier match over *code*, so anything reached
    by `getattr` counts as unread and is marked. Over-marking is visible and gets fixed;
    under-marking restores the bug.

    The scan reads code only. It used to read the file as raw text, which made every
    identifier-shaped word in a comment or docstring a use -- and the first thing that
    cost was `dispatch_timeout`, unread everywhere but named in two comments saying so.
    That is under-marking, the direction this docstring calls the dangerous one, reached
    by the sentence that documented the problem. Ignoring literals does not weaken the
    getattr rule above; it strengthens it, a name reached only through a string literal
    now being unread by both readings.
    """
    src = ROOT / "src" / "claude_delegate_local"
    used: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        if path.name == "config.py":
            continue
        used |= _code_identifiers(path.read_text(encoding="utf-8"))
    used |= _reached_through_accessors(src / "config.py", used)
    return {r["field"] for r in config.describe() if r["field"] not in used}


def _reached_through_accessors(config_path: Path, used: set[str]) -> set[str]:
    """Fields a live `config.py` accessor reads on the caller's behalf.

    One level of indirection, and only this one. `workdir_roots` is read by nothing outside
    `config.py` and is nonetheless live, because `paths.py` calls
    `effective_workdir_roots()` and that property is where the "empty means reuse
    workspace_roots" fallback lives. Inlining the fallback at the call site to satisfy a
    scanner would put a config default outside `config.py`, which is the one thing this
    project does not do.

    Marking it inert was not a false alarm in the harmless direction: the marker's stated
    meaning is that the setting "does nothing, because the subsystem it controls is not
    built", and a reader deciding whether to set it would have been told the opposite of
    the truth.

    Still conservative. An accessor nobody calls confers nothing, so an unused property
    cannot launder a dead field into a live one, and one level does not chain -- an
    accessor reached only through another accessor stays unread and stays marked.
    """
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in used:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                if inner.value.id == "self":
                    reached.add(inner.attr)
    return reached


def _cell(text: object) -> str:
    """Table-safe: escape pipes, collapse whitespace, render empties visibly."""
    s = str(text)
    if s == "":
        return "*(empty)*"
    return s.replace("|", "\\|").replace("\n", " ")


def _row(r: dict[str, object], *, inert: bool) -> str:
    """One renderer for both tables, because two of them disagreed.

    The leftover "Other" loop dropped three things the sectioned loop applied: the unit
    suffix, the **required** marker and the **Inert.** prefix. So a setting rendered
    differently depending on nothing but whether someone had filed it under a heading.
    Live for two timeouts that showed a bare number where every sibling said "seconds"
    (2026-08-28 audit). Worse latently: the footer counts inert fields across every row,
    so an inert setting landing in "Other" was counted in the total and unmarked in its
    own row -- a table contradicting itself, with the gate unable to see it because the
    committed file matched this generator exactly.
    """
    default = _cell(r["default"])
    if r["unit"]:
        default = f"{default} {r['unit']}"
    if r["required"]:
        default = "**required**"
    desc = _cell(r["description"])
    if inert:
        desc = f"**Inert.** {desc}"
    return f"| `{r['env']}` | {default} | {desc} |"


def render() -> str:
    rows = {r["field"]: r for r in config.describe()}
    unread = _unread_fields()
    placed: set[str] = set()
    out: list[str] = [
        START,
        "",
        "<!-- Generated by scripts/gen_config_docs.py from the Config dataclass in",
        "     src/claude_delegate_local/config.py. Do not edit by hand: the docs gate",
        "     regenerates this block and fails the build if it differs. To change a",
        "     default or a description, edit the dataclass field. -->",
        "",
        (f"All settings are environment variables. Prefix `{config.env_name('')}`. "
         "List-valued settings are shown comma-separated for readability, but are "
         "**parsed on `os.pathsep`** — `;` on Windows, `:` elsewhere. The rendering is "
         "deliberately platform-independent so this file is byte-identical wherever it "
         "is generated."),
        "",
        ("A description marked **Inert** means no code outside `config.py` reads that "
         "setting yet: it is validated at startup and otherwise does nothing, because the "
         "subsystem it controls is not built. See [PLAN.md](../PLAN.md) for what is where. "
         "The marker is computed by the generator from the source, not maintained by hand, "
         "so it disappears in the commit that starts using the setting."),
        "",
    ]
    for title, names in SECTIONS:
        present = [n for n in names if n in rows]
        if not present:
            continue
        out += [f"### {title}", "", "| Variable | Default | Description |",
                "| --- | --- | --- |"]
        for name in present:
            placed.add(name)
            out.append(_row(rows[name], inert=name in unread))
        out.append("")

    leftover = [n for n in rows if n not in placed]
    if leftover:
        out += ["### Other", "",
                ("<!-- These fields matched no section in gen_config_docs.py SECTIONS."
                 " Add them to a group. -->"), "",
                "| Variable | Default | Description |", "| --- | --- | --- |"]
        for name in leftover:
            out.append(_row(rows[name], inert=name in unread))
        out.append("")

    n_inert = len(unread & set(rows))
    tail = f"*{len(rows)} settings"
    tail += f", {n_inert} of them inert.*" if n_inert else ".*"
    out += [tail, "", END]
    return "\n".join(out)


def splice(existing: str, block: str) -> str:
    if START in existing and END in existing:
        head = existing.split(START, maxsplit=1)[0]
        tail = existing.split(END, 1)[1]
        return head + block + tail
    # First run, or someone removed the markers: append rather than clobber prose.
    sep = "" if existing.endswith("\n\n") or not existing else "\n"
    return existing + sep + "\n" + block + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed file is stale")
    args = ap.parse_args()

    block = render()
    existing = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    updated = splice(existing, block)

    if args.check:
        if existing == updated:
            print(f"ok: {TARGET.relative_to(ROOT)} is current "
                  f"({len(config.describe())} settings)")
            return 0
        print(f"STALE: {TARGET.relative_to(ROOT)} does not match the Config dataclass.")
        print("Run: python scripts/gen_config_docs.py\n")
        diff = difflib.unified_diff(
            existing.splitlines(), updated.splitlines(),
            fromfile="committed", tofile="generated", lineterm="", n=1,
        )
        for line in list(diff)[:40]:
            print("  " + line)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(updated, encoding="utf-8")
    verb = "updated" if existing else "created"
    print(f"{verb}: {TARGET.relative_to(ROOT)} ({len(config.describe())} settings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
