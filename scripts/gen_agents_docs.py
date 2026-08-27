#!/usr/bin/env python
"""Generate the build-time agent roster from the agent definitions themselves.

The table in `CONTRIBUTING.md` listing each subagent's model and effort is rendered from
the frontmatter in `.claude/agents/*.md`, between the GEN markers, and the docs gate fails
if the committed file differs from what this script produces.

Same anti-drift mechanism as the configuration reference (ADR-0004), applied to the same
shape of problem: a value that exists in one place and is described in another. The roster
happened to be correct when this was written, which is not the same as being kept correct.

The `effort` key is read rather than assumed. A misspelling -- `reasoning_effort` is the
tempting one -- is ignored in silence by the agent runner and quietly bills the default
tier, so a missing key is rendered as a visible `-- missing --` rather than a plausible
default.

Usage:
    python scripts/gen_agents_docs.py           # write
    python scripts/gen_agents_docs.py --check   # exit 1 if stale, print a diff
"""

from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / ".claude" / "agents"
TARGET = ROOT / "CONTRIBUTING.md"

START = "<!-- GEN:AGENTS:START -->"
END = "<!-- GEN:AGENTS:END -->"

MISSING = "-- missing --"


def frontmatter(path: Path) -> dict[str, str]:
    """The leading `---` block, as flat key/value pairs.

    Deliberately not a YAML parser: the frontmatter here is flat scalars, and pulling in a
    dependency to read four files would cost more than it explains.
    """
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def agents() -> list[dict[str, str]]:
    rows = []
    for path in sorted(AGENTS_DIR.glob("*.md")):
        fm = frontmatter(path)
        rows.append({
            "name": fm.get("name") or path.stem,
            "model": fm.get("model") or MISSING,
            "effort": fm.get("effort") or MISSING,
            "purpose": fm.get("purpose") or fm.get("description") or "",
        })
    return rows


def _short(purpose: str, limit: int = 62) -> str:
    """First sentence, trimmed. The full text is in the agent file."""
    first = re.split(r"(?<=[.!?])\s", purpose.strip())[0] if purpose.strip() else ""
    first = first.rstrip(".").replace("|", "\\|")
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def render() -> str:
    rows = agents()
    out = [
        START,
        "<!-- Generated from .claude/agents/*.md by scripts/gen_agents_docs.py."
        " Change the frontmatter, not this. -->",
        "",
        "| Agent | Model | Effort | For |",
        "|---|---|---|---|",
    ]
    for r in rows:
        out.append(f"| `{r['name']}` | {r['model']} | {r['effort']} | {_short(r['purpose'])} |")
    # No row count here: it earns its place in a 39-row config table, not a 4-row one.
    out += ["", END]
    return "\n".join(out)


def splice(existing: str, block: str) -> str:
    if START in existing and END in existing:
        return existing.split(START)[0] + block + existing.split(END, 1)[1]
    sep = "" if existing.endswith("\n\n") or not existing else "\n"
    return existing + sep + "\n" + block + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed file is stale")
    args = ap.parse_args()

    if not AGENTS_DIR.is_dir():
        print(f"no {AGENTS_DIR.relative_to(ROOT)} directory")
        return 1

    block = render()
    existing = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    updated = splice(existing, block)
    n = len(agents())

    if args.check:
        if existing == updated:
            print(f"ok: {TARGET.relative_to(ROOT)} roster is current ({n} agents)")
            return 0
        print(f"STALE: {TARGET.relative_to(ROOT)} does not match .claude/agents/.")
        print("Run: python scripts/gen_agents_docs.py\n")
        diff = difflib.unified_diff(
            existing.splitlines(), updated.splitlines(),
            fromfile="committed", tofile="generated", lineterm="", n=1,
        )
        for line in list(diff)[:40]:
            print("  " + line)
        return 1

    TARGET.write_text(updated, encoding="utf-8")
    print(f"updated: {TARGET.relative_to(ROOT)} ({n} agents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
