#!/usr/bin/env python
"""One gate, two callers: a pre-commit hook and a CI job.

Same checks either way -- the hook gives fast feedback while you work, CI is the
authority that `--no-verify` cannot walk past. Keeping it one implementation is the
point; two copies of a policy is how the policy starts disagreeing with itself.

    python scripts/docs_gate.py --mode pre-commit      # staged changes
    python scripts/docs_gate.py --mode ci              # origin/main..HEAD

BLOCK findings fail the run. WARN findings print and pass. A BLOCK can be waived with a
commit-message trailer, which is deliberately visible rather than silent:

    Docs-Gate-Skip: owning-doc -- pure rename, no behaviour change

Checks are added as the artefacts they police arrive. A check whose input does not exist
yet reports SKIP with the reason, rather than passing silently -- an invisible no-op
check is worse than an absent one, because it is trusted.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

# See JOURNAL 2026-08-25: a tool that compares committed artefacts against live source
# must never read a cached compile of that source. (mtime, size) validation misses a
# same-length edit inside one timestamp tick.
sys.pycache_prefix = tempfile.mkdtemp(prefix="cdl-gate-pyc-")

ROOT = Path(__file__).resolve().parent.parent
BLOCK, WARN, SKIP = "BLOCK", "WARN", "SKIP"

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Paths where a third party's address is legitimate and expected.
EMAIL_EXEMPT_GLOBS = ("LICENSE", "NOTICE", "docs/audits/*", "docs/specs/archive/*")

# Host-identifier shapes. These are patterns, never literals: a committed file listing
# the strings you do not want committed is itself the leak. The literals live in an
# untracked security/forbidden_strings.txt, mirrored as a CI secret.
HOST_PATTERNS: list[tuple[str, str]] = [
    (r"\b10(?:\.\d{1,3}){3}\b", "RFC1918 10.x address"),
    (r"\b192\.168(?:\.\d{1,3}){2}\b", "RFC1918 192.168.x address"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b", "RFC1918 172.16-31.x address"),
    (r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b",
     "CGNAT 100.64/10 address (Tailscale range)"),
    (r"\b[a-z0-9-]+\.ts\.net\b", "tailnet hostname"),
]

# `host:port` is only a leak when the host is a real name. Placeholders and loopback are
# how the examples are supposed to read.
HOSTPORT_RE = re.compile(r"\b([a-z][a-z0-9][a-z0-9.-]{1,40}):(\d{4,5})\b", re.I)
HOSTPORT_ALLOWED = {
    "localhost", "127.0.0.1", "0.0.0.0", "example.com", "example.org",
    "your-head-node", "head", "host", "hostname", "some-host", "127.0.0.1",
}

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".txt", ".yaml", ".yml", ".json", ".cfg", ".ini",
    ".sh", ".ps1", ".example", "", ".gitignore", ".gitattributes",
}


@dataclass
class Finding:
    level: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:5} [{self.check}] {self.message}"


def run(*args: str) -> str:
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def changed_files(mode: str, diff_range: str | None) -> list[str]:
    if mode == "pre-commit":
        out = run("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        rng = diff_range or "origin/main...HEAD"
        out = run("git", "diff", "--name-only", "--diff-filter=ACMR", rng)
        if not out:  # first push, or no upstream yet: fall back to everything tracked
            out = run("git", "ls-files")
    return [line for line in out.splitlines() if line]


def tracked_text_files() -> list[Path]:
    files = []
    for rel in run("git", "ls-files").splitlines():
        p = ROOT / rel
        if p.exists() and p.suffix.lower() in TEXT_SUFFIXES and p.stat().st_size < 2_000_000:
            files.append(p)
    return files


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


# --------------------------------------------------------------------------- checks


def check_commit_identity(mode: str, diff_range: str | None) -> list[Finding]:
    """The check that actually protects a public repo.

    Content scanners never see the author field, and the global identity on a work
    machine is usually the work address. This is ~15 lines and it is the difference
    between a private address staying private and it being on every commit forever.
    """
    allowed = set(load_lines(ROOT / "security" / "allowed_emails.txt"))
    if not allowed:
        return [Finding(BLOCK, "identity",
                        "security/allowed_emails.txt lists no addresses, so no commit "
                        "author can be validated. Add the intended address.")]
    if mode == "pre-commit":
        pairs = [(run("git", "config", "user.email"), "pending commit")]
    else:
        rng = diff_range or "origin/main...HEAD"
        raw = run("git", "log", "--format=%ae%x00%ce%x00%h", rng)
        pairs = []
        for line in raw.splitlines():
            if not line:
                continue
            ae, ce, sha = line.split("\x00")
            pairs.append((ae, sha))
            if ce != ae:
                pairs.append((ce, f"{sha} (committer)"))
    out = []
    for addr, where in pairs:
        if addr and addr not in allowed:
            out.append(Finding(
                BLOCK, "identity",
                f"{where}: author {addr!r} is not in security/allowed_emails.txt. "
                f"This repo is public. Check `git config user.email` inside the repo, "
                f"and that the includeIf in ~/.gitconfig names this directory."))
    return out


def check_emails_in_files() -> list[Finding]:
    allowed = set(load_lines(ROOT / "security" / "allowed_emails.txt"))
    out = []
    for p in tracked_text_files():
        r = rel(p)
        if any(fnmatch.fnmatch(r, g) for g in EMAIL_EXEMPT_GLOBS):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in EMAIL_RE.finditer(line):
                if m.group(0) not in allowed:
                    out.append(Finding(
                        BLOCK, "email-content",
                        f"{r}:{i} contains {m.group(0)!r}, which is not in "
                        f"security/allowed_emails.txt. Allowlisted, not denylisted, so "
                        f"an address nobody thought to list is still caught."))
    return out


def check_host_identifiers() -> list[Finding]:
    literals = load_lines(ROOT / "security" / "forbidden_strings.txt")
    out = []
    for p in tracked_text_files():
        r = rel(p)
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            for pat, label in HOST_PATTERNS:
                m = re.search(pat, line, re.I)
                if m:
                    out.append(Finding(BLOCK, "host-identifier",
                                       f"{r}:{i} looks like a {label}: {m.group(0)!r}. "
                                       f"Hosts are configuration, never literals."))
            for m in HOSTPORT_RE.finditer(line):
                host = m.group(1).lower()
                if host not in HOSTPORT_ALLOWED and not host.startswith("your-"):
                    out.append(Finding(
                        BLOCK, "host-identifier",
                        f"{r}:{i} contains {m.group(0)!r}. A hostname is as identifying "
                        f"as an address. Use a placeholder in examples."))
            for lit in literals:
                if lit.lower() in line.lower():
                    out.append(Finding(BLOCK, "host-identifier",
                                       f"{r}:{i} matches an entry in the local "
                                       f"forbidden_strings list."))
    if not literals:
        out.append(Finding(SKIP, "host-identifier",
                           "security/forbidden_strings.txt absent, so only the committed "
                           "patterns ran. Create it locally (untracked) with your host's "
                           "literal names for exact matching."))
    return out


def check_secret_paths() -> list[Finding]:
    globs = load_lines(ROOT / "security" / "secret_globs.txt")
    if not globs:
        return [Finding(BLOCK, "secret-path", "security/secret_globs.txt is empty.")]
    out = []
    for r in run("git", "ls-files").splitlines():
        name = Path(r).name
        # The policy files are not the thing the policy is about. secret_globs.txt
        # matches its own '*secret*' pattern, which is the gate's first self-inflicted
        # false positive and a fair warning about pattern lists that describe themselves.
        # Safe to exempt: forbidden_strings.txt is gitignored so it can never be tracked,
        # and the rest of security/ is patterns and prose.
        if r.startswith("security/"):
            continue
        for g in globs:
            if fnmatch.fnmatch(r, g) or fnmatch.fnmatch(name, g):
                if r.endswith(".example"):
                    continue
                out.append(Finding(BLOCK, "secret-path",
                                   f"{r} is tracked but matches secret glob {g!r}."))
                break
    return out


def check_generated_docs() -> list[Finding]:
    out = []
    generators = [
        ("scripts/gen_config_docs.py", "docs/CONFIGURATION.md"),
        ("scripts/gen_status.py", "STATUS.md"),
    ]
    for script, target in generators:
        if not (ROOT / script).exists():
            out.append(Finding(SKIP, "generated-doc", f"{script} not written yet."))
            continue
        proc = subprocess.run([sys.executable, script, "--check"],
                              cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            first = (proc.stdout or proc.stderr).strip().splitlines()
            out.append(Finding(BLOCK, "generated-doc",
                               f"{target} is stale. Run: python {script}. "
                               f"({first[0] if first else 'no output'})"))
    return out


def check_budgets() -> list[Finding]:
    """Budgets block, but the block never means delete. See ADR-0003.

    Two kinds, because one rule does not fit both document classes (ADR-0022):

    `BUDGET: n`           total lines. For MUTABLE documents, where exceeding the cap is a
                          real prompt to ask whether everything still earns its place.
    `BUDGET-PER-ENTRY: n` longest `## ` section. For APPEND-ONLY documents, where history
                          does not stop earning its place, so a total cap could only ever
                          be raised. Capping each entry keeps them terse instead.
    `ARCHIVE-AT: n`       optional, append-only only. Warns when the file has grown enough
                          to split by year, rather than blocking.
    """
    out = []
    seen = 0
    for p in sorted(ROOT.rglob("*.md")):
        r = rel(p)
        if ".git" in p.parts or "docs/audits" in r or "archive" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        total = len(text.splitlines())

        archive_at = re.search(r"<!--\s*ARCHIVE-AT:\s*(\d+)\s*-->", text)
        if archive_at and total > int(archive_at.group(1)):
            out.append(Finding(
                WARN, "budget",
                f"{r} is {total} lines, past its archive threshold of "
                f"{archive_at.group(1)}. Consider splitting the older entries into a "
                f"dated file; do not trim them."))

        per_entry = re.search(r"<!--\s*BUDGET-PER-ENTRY:\s*(\d+)\s*-->", text)
        if per_entry:
            seen += 1
            cap = int(per_entry.group(1))
            sections = re.split(r"^## ", text, flags=re.M)[1:]
            for sec in sections:
                title = sec.splitlines()[0].strip()[:70] if sec.strip() else "(untitled)"
                n = len(sec.splitlines())
                if n > cap:
                    out.append(Finding(
                        BLOCK, "budget",
                        f"{r}: entry {title!r} is {n} lines against a per-entry budget of "
                        f"{cap}. Append-only files cap each entry, not the total -- trim "
                        f"this one, or raise the per-entry budget with a reason in this "
                        f"same commit."))
            continue

        m = re.search(r"<!--\s*BUDGET:\s*(\d+)\s*-->", text)
        if not m:
            if archive_at:
                seen += 1
            continue
        seen += 1
        budget = int(m.group(1))
        if total > budget:
            out.append(Finding(
                BLOCK, "budget",
                f"{r} is {total} lines against a budget of {budget}. Three ways out, and "
                f"none of them is deleting something valuable: trim real redundancy; "
                f"split it (only for a different audience, different owned code, or "
                f"reference-vs-narrative); or raise the budget with a one-line reason in "
                f"this same commit."))
    if not seen:
        out.append(Finding(SKIP, "budget", "no document declares a budget header yet."))
    return out


def check_adr_format() -> list[Finding]:
    """The ADR headings ARE the index, so their shape is load-bearing."""
    p = ROOT / "DECISIONS.md"
    if not p.exists():
        return [Finding(SKIP, "adr", "DECISIONS.md not written yet.")]
    text = p.read_text(encoding="utf-8")
    heads = re.findall(r"^## (.+)$", text, re.M)
    if not heads:
        return [Finding(BLOCK, "adr", "DECISIONS.md declares no ADR headings.")]
    pattern = re.compile(
        r"^(?:~~)?ADR-(\d{4}) — \d{4}-\d{2}-\d{2} — .+?(?:~~)? — "
        r"(Accepted|Proposed|Rejected|Superseded by ADR-\d{4}|"
        r"Partially superseded by ADR-\d{4})$"
    )
    out, numbers = [], []
    # Collect DECLARED numbers first. Searching the file text for "ADR-0099" cannot
    # work: the reference being validated contains that string itself, so the check
    # would always find its own needle and never fire. Caught by a negative test --
    # which is the argument for writing them.
    declared = {
        int(m.group(1))
        for h in heads
        if (m := re.match(r"^(?:~~)?ADR-(\d{4}) ", h.strip()))
    }
    for h in heads:
        m = pattern.match(h.strip())
        if not m:
            out.append(Finding(BLOCK, "adr",
                               f"malformed ADR heading: {h[:90]!r}. Expected "
                               f"'ADR-NNNN — YYYY-MM-DD — title — Status'."))
            continue
        numbers.append(int(m.group(1)))
        ref = re.search(r"[Ss]uperseded by ADR-(\d{4})", h)
        if ref and int(ref.group(1)) not in declared:
            out.append(Finding(BLOCK, "adr",
                               f"ADR-{m.group(1)} points at ADR-{ref.group(1)}, which "
                               f"is not declared by any heading in this file."))
        if "~~" in h and "Superseded by" not in h:
            out.append(Finding(BLOCK, "adr",
                               f"ADR-{m.group(1)} is struck through but names no "
                               f"replacement."))
    if numbers != sorted(numbers, reverse=True):
        out.append(Finding(BLOCK, "adr", "ADRs are not in descending order."))
    dupes = {n for n in numbers if numbers.count(n) > 1}
    if dupes:
        out.append(Finding(BLOCK, "adr", f"duplicate ADR numbers: {sorted(dupes)}."))
    return out


MANIFEST = ROOT / "scripts" / "docs_ownership.toml"


def load_manifest() -> dict | None:
    if not MANIFEST.exists():
        return None
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def _matches(path: str, globs: list[str]) -> bool:
    """Glob match that treats ** as spanning directories, which fnmatch does not."""
    for g in globs:
        if fnmatch.fnmatch(path, g):
            return True
        if g.endswith("/**") and (path == g[:-3] or path.startswith(g[:-2])):
            return True
    return False


def owners_of(path: str, manifest: dict) -> list[str]:
    return [doc for doc, meta in manifest["docs"].items()
            if _matches(path, meta.get("owns", []))]


def check_ownership(changed: list[str]) -> list[Finding]:
    """Changed code must be accompanied by its owning document, in the same commit.

    This is what makes the one-feature-per-commit rule mean something: without it there
    is nothing for the check to compare against.
    """
    manifest = load_manifest()
    if manifest is None:
        return [Finding(SKIP, "owning-doc", f"{MANIFEST.name} not present.")]

    unowned = manifest.get("unowned", {}).get("paths", [])
    changed_set = set(changed)
    out = []
    stale_pairs: dict[str, list[str]] = {}

    for path in changed:
        if not path.startswith(("src/", "scripts/", ".github/")):
            continue
        if _matches(path, unowned):
            continue
        docs = owners_of(path, manifest)
        if not docs:
            out.append(Finding(
                WARN, "owning-doc",
                f"{path} has no owning document and is not listed as unowned in "
                f"{MANIFEST.name}. Assign it or declare it unowned -- an unassigned file "
                f"is a documentation gap nobody can see."))
            continue
        # A generated document is satisfied by regeneration, which the generated-doc
        # check polices separately. Requiring a manual edit there would be theatre.
        if all(manifest["docs"][d].get("generated") for d in docs):
            continue
        if not any(d in changed_set for d in docs):
            stale_pairs.setdefault(", ".join(docs), []).append(path)

    for docs, paths in stale_pairs.items():
        shown = ", ".join(paths[:3]) + (f" (+{len(paths)-3} more)" if len(paths) > 3 else "")
        out.append(Finding(
            BLOCK, "owning-doc",
            f"{shown} changed but {docs} was not updated in the same commit. Update it, "
            f"or add a trailer: 'Docs-Gate-Skip: owning-doc -- <reason>'."))
    return out


def check_orphan_docs() -> list[Finding]:
    manifest = load_manifest()
    if manifest is None:
        return [Finding(SKIP, "orphan-doc", f"{MANIFEST.name} not present.")]
    tracked = set(run("git", "ls-files").splitlines())
    out = []
    for doc, meta in manifest["docs"].items():
        owns = meta.get("owns", [])
        if not owns:
            continue
        if not any(_matches(f, owns) for f in tracked):
            out.append(Finding(
                WARN, "orphan-doc",
                f"{doc} claims ownership of {owns} but none of those paths exist. Either "
                f"the code has not been written yet, or the document should be deleted "
                f"and its manifest entry removed."))
    return out


def check_manifest_docs_exist() -> list[Finding]:
    """A manifest entry must name a document that exists.

    Catches both directions: an entry added before its document is written, and a document
    deleted without removing its entry. Either leaves the ownership map describing a repo
    that is not this one.
    """
    manifest = load_manifest()
    if manifest is None:
        return [Finding(SKIP, "manifest", f"{MANIFEST.name} not present.")]
    out = []
    for doc, meta in manifest["docs"].items():
        if not (ROOT / doc).exists():
            kind = "generated" if meta.get("generated") else "hand-written"
            out.append(Finding(
                WARN, "manifest",
                f"{MANIFEST.name} lists {doc} ({kind}) but the file does not exist. Write "
                f"it, or remove the entry -- an ownership map that names absent documents "
                f"describes a repository other than this one."))
    return out


def check_split_dodge(changed: list[str]) -> list[Finding]:
    """A new document must earn its existence, or it is a budget being evaded.

    Valid reasons to split: a different audience, different owned code, or reference
    material separated from narrative. "It got long" is a reason to raise a budget, not
    to create a file. See ADR-0003.
    """
    manifest = load_manifest()
    if manifest is None:
        return [Finding(SKIP, "split-dodge", f"{MANIFEST.name} not present.")]
    added = set(run("git", "diff", "--cached", "--name-only",
                    "--diff-filter=A").splitlines())
    out = []
    for doc in added:
        meta = manifest["docs"].get(doc)
        if meta is None:
            continue
        aud, owns = set(meta.get("audience", [])), set(meta.get("owns", []))
        for other, om in manifest["docs"].items():
            if other == doc or om.get("plane") != meta.get("plane"):
                continue
            o_aud, o_owns = set(om.get("audience", [])), set(om.get("owns", []))
            if aud and aud <= o_aud and owns and owns <= o_owns:
                out.append(Finding(
                    BLOCK, "split-dodge",
                    f"new document {doc} has the same audience and a subset of the code "
                    f"owned by {other}, so it is not a split -- it is a size budget "
                    f"being evaded. Raise {other}'s budget with a reason instead, or give "
                    f"{doc} a distinct audience or distinct owned code."))
                break
    return out


CHECKS = {
    "identity": check_commit_identity,
    "email-content": check_emails_in_files,
    "host-identifier": check_host_identifiers,
    "secret-path": check_secret_paths,
    "generated-doc": check_generated_docs,
    "budget": check_budgets,
    "adr": check_adr_format,
    "owning-doc": check_ownership,
    "orphan-doc": check_orphan_docs,
    "split-dodge": check_split_dodge,
    "manifest": check_manifest_docs_exist,
}


def waivers(mode: str) -> dict[str, str]:
    """Parse `Docs-Gate-Skip: <check> -- <reason>` trailers.

    Waivers are echoed in the output on purpose. An escape hatch nobody can see becomes
    the default route; one that leaves a visible trail in every run does not.
    """
    if mode == "pre-commit":
        msg_file = ROOT / ".git" / "COMMIT_EDITMSG"
        text = msg_file.read_text(encoding="utf-8", errors="replace") if msg_file.exists() else ""
    else:
        text = run("git", "log", "--format=%B", "origin/main...HEAD")
    out = {}
    for m in re.finditer(r"^Docs-Gate-Skip:\s*([a-z-]+)\s*(?:--|—)\s*(.+)$", text, re.M):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("pre-commit", "ci"), default="pre-commit")
    ap.add_argument("--diff", dest="diff_range", default=None)
    ap.add_argument("--owner", metavar="PATH",
                    help="print which document owns PATH, then exit. Exists so CLAUDE.md "
                         "can state the ownership rule without restating the manifest.")
    args = ap.parse_args()

    if args.owner:
        manifest = load_manifest()
        if manifest is None:
            print(f"no manifest at {MANIFEST}")
            return 1
        path = args.owner.replace("\\", "/")
        docs = owners_of(path, manifest)
        if docs:
            for d in docs:
                gen = " (generated -- run its generator, do not hand-edit)"                       if manifest["docs"][d].get("generated") else ""
                print(f"{d}{gen}")
            return 0
        if _matches(path, manifest.get("unowned", {}).get("paths", [])):
            print(f"{path} is declared unowned in {MANIFEST.name}")
            return 0
        print(f"{path} has no owning document and is not declared unowned -- "
              f"assign it in {MANIFEST.name}")
        return 1

    changed = changed_files(args.mode, args.diff_range)
    waived = waivers(args.mode)

    findings: list[Finding] = []
    for name, fn in CHECKS.items():
        if name == "identity":
            findings += fn(args.mode, args.diff_range)
        elif name in ("owning-doc", "split-dodge"):
            findings += fn(changed)
        else:
            findings += fn()

    blocks = [f for f in findings if f.level == BLOCK and f.check not in waived]
    waived_hits = [f for f in findings if f.level == BLOCK and f.check in waived]
    warns = [f for f in findings if f.level == WARN]
    skips = [f for f in findings if f.level == SKIP]

    print(f"docs gate: mode={args.mode}, {len(changed)} changed file(s)")
    for f in blocks + warns:
        print("  " + str(f))
    for f in waived_hits:
        print(f"  WAIVED [{f.check}] {f.message}")
        print(f"         reason given: {waived[f.check]}")
    for f in skips:
        print("  " + str(f))

    if blocks:
        print(f"\nFAIL: {len(blocks)} blocking finding(s).")
        return 1
    print(f"\nPASS ({len(warns)} warning(s), {len(skips)} not-yet-applicable check(s)"
          + (f", {len(waived_hits)} waived" if waived_hits else "") + ").")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
