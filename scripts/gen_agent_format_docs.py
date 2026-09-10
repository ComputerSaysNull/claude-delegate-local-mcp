#!/usr/bin/env python
"""Render the agent-file format reference in `docs/AGENTS.md` from the shipped spec.

The format had one home, `docs/AGENTS.md`, and that home does not travel: measured on
2026-09-10, the wheel carries 21 modules and nothing else, so a host holding only the
package cannot read it. M10 asks for exactly the opposite. The spec therefore moved
*inside* the package, to the `write-delegate-agent` skill, which ships.

That would have made two copies, which is the drift CLAUDE.md exists to stop. So this is
the same anti-drift mechanism as the configuration reference (ADR-0004) and the agent
roster: one source, rendered into the document, and the gate fails when the committed text
disagrees.

**Two blocks, not one**, because `docs/AGENTS.md` interleaves the reference with the
reasoning behind it -- why `model` genuinely binds, why an over-cap `max_turns` is refused
where a caller's is clamped. Splicing one contiguous region would have meant moving that
reasoning to suit the generator. The reference is generated; every "why" paragraph around
it stays hand-written and in place.

Sections are matched by heading text rather than by line number, so editing the prose
around them cannot silently change what is spliced.

Usage:
    python scripts/gen_agent_format_docs.py           # write
    python scripts/gen_agent_format_docs.py --check   # exit 1 if stale, print a diff
"""

from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src/claude_delegate_local/skills/write-delegate-agent/SKILL.md"
TARGET = ROOT / "docs/AGENTS.md"

SOURCE_REL = "src/claude_delegate_local/skills/write-delegate-agent/SKILL.md"

# Which of the spec's sections each block carries. A section named here and absent from the
# source is an error rather than an empty block -- see `sections()`.
BLOCKS: dict[str, tuple[str, ...]] = {
    "AGENT-FORMAT-LOCATIONS": ("Where the file goes",),
    "AGENT-FORMAT-FIELDS": ("Frontmatter", "The body", "Validate it"),
}


def markers(tag: str) -> tuple[str, str]:
    return f"<!-- GEN:{tag}:START -->", f"<!-- GEN:{tag}:END -->"


def sections(text: str) -> dict[str, str]:
    """The source's `##` sections, keyed by heading text.

    The frontmatter and the lead-in prose above the first heading are deliberately not
    returned: they are the skill's own framing -- what it is for, that it is its own worked
    example -- and belong to the skill rather than to the reference.
    """
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^## +(.*?)\s*$", line)
        if m:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = [line]
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def render(tag: str, found: dict[str, str]) -> str:
    start, end = markers(tag)
    wanted = BLOCKS[tag]
    missing = [name for name in wanted if name not in found]
    if missing:
        raise SystemExit(
            f"{SOURCE_REL} has no '## {missing[0]}' section, which block {tag} is built "
            f"from. Sections found: {sorted(found)}"
        )
    parts = [
        start,
        (
            f"<!-- Generated from {SOURCE_REL} by scripts/gen_agent_format_docs.py."
            " That file ships inside the package; edit it, not this. -->"
        ),
        "",
    ]
    parts.append("\n\n".join(found[name] for name in wanted))
    parts += ["", end]
    return "\n".join(parts)


def splice(existing: str, tag: str, block: str) -> str:
    start, end = markers(tag)
    if start in existing and end in existing:
        return existing.split(start, maxsplit=1)[0] + block + existing.split(end, 1)[1]
    raise SystemExit(
        f"{TARGET.relative_to(ROOT)} has no {start} / {end} pair. Add the markers where "
        "the block belongs; this script will not guess a location inside a document it "
        "does not otherwise own."
    )


def build() -> tuple[str, str]:
    found = sections(SOURCE.read_text(encoding="utf-8"))
    existing = TARGET.read_text(encoding="utf-8")
    updated = existing
    for tag in BLOCKS:
        updated = splice(updated, tag, render(tag, found))
    return existing, updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args()

    if not SOURCE.is_file():
        print(f"no {SOURCE_REL}")
        return 1

    existing, updated = build()
    n = len(BLOCKS)

    if args.check:
        if existing == updated:
            print(f"ok: {TARGET.relative_to(ROOT)} matches {SOURCE_REL} ({n} blocks)")
            return 0
        print(f"STALE: {TARGET.relative_to(ROOT)} does not match {SOURCE_REL}.")
        print("Run: python scripts/gen_agent_format_docs.py\n")
        diff = difflib.unified_diff(
            existing.splitlines(),
            updated.splitlines(),
            fromfile="committed",
            tofile="generated",
            lineterm="",
            n=1,
        )
        for line in list(diff)[:40]:
            print("  " + line)
        return 1

    TARGET.write_text(updated, encoding="utf-8")
    print(f"updated: {TARGET.relative_to(ROOT)} ({n} blocks from {SOURCE_REL})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
