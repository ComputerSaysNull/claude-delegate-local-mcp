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
import json
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
    # Private-DNS suffixes generally, not one vendor: wider protection, and
    # it does not advertise which kind of network sits behind it.
    (r"\b[a-z0-9-]+\.(ts\.net|internal|lan|home\.arpa|localdomain)\b",
     "private-network hostname"),
]

# `host:port` is only a leak when the host is a real name. Placeholders and loopback are
# how the examples are supposed to read.
HOSTPORT_RE = re.compile(r"\b([a-z][a-z0-9][a-z0-9.-]{1,40}):(\d{4,5})\b", re.I)
HOSTPORT_ALLOWED = {
    "localhost", "127.0.0.1", "0.0.0.0", "example.com", "example.org",
    "your-head-node", "head", "host", "hostname", "some-host", "127.0.0.1",
}

# Extensions that are certainly binary. Everything else is scanned, including file types
# nobody thought of. This used to be an allowlist of "text" suffixes, which was fail-OPEN:
# a host literal in a .rst, .ts or .sql file passed silently because the extension was not
# on the list. An unknown extension must be scanned, not skipped.
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
    ".pdf", ".zip", ".gz", ".bz2", ".xz", ".tar", ".7z", ".whl", ".jar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".pyc", ".pyd",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm",
    ".db", ".sqlite", ".sqlite3", ".mo",
}
MAX_SCAN_BYTES = 2_000_000


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


def scannable_files() -> list[Path]:
    """Every tracked file that is not provably binary.

    Binary detection is by content, not just extension: a NUL byte in the first 8 KiB.
    Extension alone is a guess, and guessing wrong here means skipping a file that holds
    the thing being looked for.
    """
    files = []
    for r in run("git", "ls-files").splitlines():
        p = ROOT / r
        if not p.exists() or p.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if p.stat().st_size > MAX_SCAN_BYTES:
                continue
            with open(p, "rb") as fh:
                if b"\x00" in fh.read(8192):
                    continue
        except OSError:
            continue
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
    allowed = set(load_lines(AUTHOR_ALLOWLIST_FILE))
    if not allowed:
        return [Finding(BLOCK, "identity",
                        "security/allowed_emails.txt lists no addresses, so no commit "
                        "author can be validated. Add the intended address.")]
    if mode == "pre-commit":
        pairs = [(run("git", "config", "user.email"), "pending commit")]
    else:
        rng = diff_range or "origin/main...HEAD"
        # --no-merges: for a pull_request event, Actions checks out a synthetic merge
        # commit authored by GitHub itself (noreply@github.com). That is not a
        # contribution, and flagging it would block every pull request forever. This
        # project squash-merges, so real merge commits do not appear in history either.
        raw = run("git", "log", "--no-merges", "--format=%ae%x00%ce%x00%h", rng)
        pairs = []
        for line in raw.splitlines():
            if not line:
                continue
            ae, ce, sha = line.split("\x00")
            pairs.append((ae, sha))
            # GitHub is the committer on every squash merge it performs. That
            # is its machinery, not an identity claim, so it is permitted as a
            # committer and still refused as an author -- the author field is
            # the claim that matters.
            if ce != ae and ce != "noreply" + "@github.com":
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


# Addresses that identify nobody live in a data file, not here: gitleaks needs the same
# set, and a second hand-maintained copy is the drift this project exists to prevent.
# scripts/gen_gitleaks_config.py renders .gitleaks.toml from these same lists.
CONTENT_SAFE_FILE = ROOT / "security" / "content_safe_emails.txt"
AUTHOR_ALLOWLIST_FILE = ROOT / "security" / "allowed_emails.txt"


# RFC 2606 reserves these domains for documentation and examples. An address there
# identifies nobody by construction, so this is a rule rather than a list -- listing
# individual example addresses invites an endless queue of one-line additions, each of
# which is a chance to add a real one by mistake.
RESERVED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.edu")


def is_content_safe(addr: str) -> bool:
    if addr in content_safe_emails():
        return True
    return addr.lower().rsplit("@", 1)[-1] in RESERVED_EMAIL_DOMAINS


def content_safe_emails() -> set[str]:
    return set(load_lines(CONTENT_SAFE_FILE))


def check_emails_in_files() -> list[Finding]:
    allowed = set(load_lines(AUTHOR_ALLOWLIST_FILE)) | content_safe_emails()
    out = []
    for p in scannable_files():
        r = rel(p)
        if any(fnmatch.fnmatch(r, g) for g in EMAIL_EXEMPT_GLOBS):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in EMAIL_RE.finditer(line):
                if m.group(0) not in allowed and not is_content_safe(m.group(0)):
                    out.append(Finding(
                        BLOCK, "email-content",
                        f"{r}:{i} contains {m.group(0)!r}, which is neither in "
                        f"security/allowed_emails.txt nor a known-generic address. "
                        f"Allowlisted, not denylisted, so an address nobody thought to "
                        f"list is still caught."))
    return out


# A literal prefixed with "word:" is matched case-sensitively on word boundaries
# instead of case-insensitively as a substring. It exists for the case the plain form
# cannot serve: a name that is also an ordinary programming word.
#
# The reasoning that first rejected this was wrong, and wrong in an instructive way. A
# common surname looked unusably noisy because it appeared hundreds of times -- but every
# one of those was inside a virtualenv, which is not tracked and is never scanned. Over
# the files the gate actually reads, the capitalised whole word appeared zero times. The
# measurement was of the wrong population.
WORD_PREFIX = "word:"


def _word_pattern(literal: str) -> str:
    return r"(?<![A-Za-z0-9_])" + re.escape(literal) + r"(?![A-Za-z0-9_])"


def split_literals(literals: list[str]) -> tuple[list[str], list[str]]:
    """Into (loose, word-boundary). Loose is the default and covers hostnames."""
    loose, words = [], []
    for lit in literals:
        (words if lit.startswith(WORD_PREFIX) else loose).append(
            lit[len(WORD_PREFIX):].strip() if lit.startswith(WORD_PREFIX) else lit)
    return loose, words


def check_host_identifiers() -> list[Finding]:
    all_literals = load_lines(ROOT / "security" / "forbidden_strings.txt")
    literals, word_literals = split_literals(all_literals)
    out = []
    for p in scannable_files():
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
            # Compare with whitespace normalised, so "Ada  Lovelace" matches
            # "Ada Lovelace". Harmless for single-word entries, which contain no
            # whitespace to collapse. Without this the line pass missed the case and the
            # whole-file pass then suppressed it as an apparent duplicate -- a dedup
            # guard hiding a genuine finding.
            flat_line = " ".join(line.split()).lower()
            for lit in literals:
                if " ".join(lit.split()).lower() in flat_line:
                    out.append(Finding(BLOCK, "host-identifier",
                                       f"{r}:{i} matches an entry in the local "
                                       f"forbidden_strings list."))
            for lit in word_literals:
                if re.search(_word_pattern(lit), line):
                    out.append(Finding(BLOCK, "host-identifier",
                                       f"{r}:{i} matches a word-boundary entry in the "
                                       f"local forbidden_strings list."))
    # Multi-word literals need a second pass. A name is written across a line break,
    # or with a double space, far more often than a hostname is -- and the line-by-line
    # pass above cannot see either. Normalising all whitespace to single spaces catches
    # both. Single-token literals are already handled and are skipped here.
    multiword = [lit for lit in literals if len(lit.split()) > 1]
    if multiword:
        for p2 in scannable_files():
            r2 = rel(p2)
            flat = " ".join(p2.read_text(encoding="utf-8", errors="replace").split()).lower()
            for lit in multiword:
                norm = " ".join(lit.split()).lower()
                if norm in flat and not any(
                    norm in " ".join(ln.split()).lower()
                    for ln in p2.read_text(encoding="utf-8", errors="replace").splitlines()
                ):
                    out.append(Finding(
                        BLOCK, "host-identifier",
                        f"{r2} contains a multi-word entry from the local "
                        f"forbidden_strings list, split across lines or spacing."))
    if not literals:
        out.append(Finding(SKIP, "host-identifier",
                           "security/forbidden_strings.txt absent, so only the committed "
                           "patterns ran. Create it locally (untracked) with your host's "
                           "literal names for exact matching."))
    return out


# Files in security/ that ARE the policy, and so cannot be judged by it.
POLICY_FILES = {
    "security/secret_globs.txt",
    "security/allowed_emails.txt",
    "security/content_safe_emails.txt",
    "security/README.md",
}

# Files that must never be tracked under any circumstances, whatever .gitignore says.
# Belt and braces on purpose: .gitignore is one edit away from not covering something,
# and `git add -f` ignores it entirely.
NEVER_TRACK = {
    "security/forbidden_strings.txt",
    ".env",
    "models.toml",
}


def check_never_tracked() -> list[Finding]:
    """A second, independent layer under .gitignore.

    The file listing host literals must not enter the repository even if .gitignore is
    edited, even if someone uses `git add -f`, and even when it is empty -- an empty one
    committed today gets populated tomorrow in a commit nobody looks at twice.
    """
    tracked = set(run("git", "ls-files").splitlines())
    staged = set(run("git", "diff", "--cached", "--name-only").splitlines())
    out = []
    for path in sorted(NEVER_TRACK & (tracked | staged)):
        out.append(Finding(
            BLOCK, "never-track",
            f"{path} is tracked or staged and must never be. It holds machine-specific "
            f"values that identify you. Run: git rm --cached {path}"))
    return out


def check_commit_message(mode: str, diff_range: str | None) -> list[Finding]:
    """Scan the commit message itself.

    CLAUDE.md stated the gate blocked host literals in "code, docs, tests or a commit
    message". It did not: the only place the message was read was to parse waiver
    trailers. A document promising protection that does not exist is worse than no
    promise, because it is relied upon. A message is also the easiest place to leak a
    hostname -- you are describing the thing you were just debugging.
    """
    if mode == "pre-commit":
        f = ROOT / ".git" / "COMMIT_EDITMSG"
        texts = [("pending commit", f.read_text(encoding="utf-8", errors="replace"))] if f.exists() else []
    else:
        rng = diff_range or "origin/main...HEAD"
        raw = run("git", "log", "--no-merges", "--format=%h%x1f%B%x1e", rng)
        texts = []
        for chunk in raw.split(chr(30)):
            if chr(31) in chunk:
                sha, body = chunk.split(chr(31), 1)
                texts.append((sha.strip(), body))

    literals, word_literals = split_literals(
        load_lines(ROOT / "security" / "forbidden_strings.txt"))
    allowed = set(load_lines(AUTHOR_ALLOWLIST_FILE))
    out = []
    for where, text in texts:
        # A comment line in a commit template is not part of the message.
        body = chr(10).join(ln for ln in text.splitlines() if not ln.startswith("#"))
        flat = " ".join(body.split()).lower()
        for lit in literals:
            if " ".join(lit.split()).lower() in flat:
                out.append(Finding(BLOCK, "commit-message",
                                   f"{where}: the message contains an entry from the "
                                   f"local forbidden_strings list."))
                break
        for lit in word_literals:
            if re.search(_word_pattern(lit), body):
                out.append(Finding(BLOCK, "commit-message",
                                   f"{where}: the message contains a word-boundary "
                                   f"entry from the local forbidden_strings list."))
                break
        for pat, label in HOST_PATTERNS:
            m = re.search(pat, body, re.I)
            if m:
                out.append(Finding(BLOCK, "commit-message",
                                   f"{where}: the message contains what looks like a "
                                   f"{label}: {m.group(0)!r}."))
        for m in EMAIL_RE.finditer(body):
            if m.group(0) not in allowed and not is_content_safe(m.group(0)):
                out.append(Finding(BLOCK, "commit-message",
                                   f"{where}: the message contains {m.group(0)!r}, "
                                   f"which is not an allowlisted address."))
    return out


def scan_text(label: str, text: str) -> list[Finding]:
    """Run the identifier checks over arbitrary text.

    Used for the commit message and for a pull request title and body. A pull request
    body is a public surface written outside git entirely, so no hook and no file check
    can see it -- which is exactly how a specimen got published once despite every other
    check passing.
    """
    loose, words = split_literals(
        load_lines(ROOT / "security" / "forbidden_strings.txt"))
    allowed = set(load_lines(AUTHOR_ALLOWLIST_FILE))
    flat = " ".join(text.split()).lower()
    out = []
    for lit in loose:
        if " ".join(lit.split()).lower() in flat:
            out.append(Finding(BLOCK, "public-text",
                               f"{label} contains an entry from the local "
                               f"forbidden_strings list."))
            break
    for lit in words:
        if re.search(_word_pattern(lit), text):
            out.append(Finding(BLOCK, "public-text",
                               f"{label} contains a word-boundary entry from the local "
                               f"forbidden_strings list."))
            break
    for pat, lbl in HOST_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            out.append(Finding(BLOCK, "public-text",
                               f"{label} contains what looks like a {lbl}: "
                               f"{m.group(0)!r}."))
    for m in EMAIL_RE.finditer(text):
        if m.group(0) not in allowed and not is_content_safe(m.group(0)):
            out.append(Finding(BLOCK, "public-text",
                               f"{label} contains {m.group(0)!r}, which is not an "
                               f"allowlisted address."))
    return out


def check_pr_text(event_path: str | None) -> list[Finding]:
    """Scan a pull request title and body from the Actions event payload."""
    if not event_path or not Path(event_path).exists():
        return [Finding(SKIP, "public-text",
                        "no pull request event payload; title and body not scanned.")]
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [Finding(WARN, "public-text", f"could not read the event payload: {e}")]
    pr = payload.get("pull_request") or {}
    out = []
    out += scan_text("the pull request title", pr.get("title") or "")
    out += scan_text("the pull request body", pr.get("body") or "")
    if not out:
        out.append(Finding(SKIP, "public-text", "pull request title and body are clean."))
    return out


def check_secret_paths() -> list[Finding]:
    globs = load_lines(ROOT / "security" / "secret_globs.txt")
    if not globs:
        return [Finding(BLOCK, "secret-path", "security/secret_globs.txt is empty.")]
    out = []
    for r in run("git", "ls-files").splitlines():
        name = Path(r).name
        # Exempt the policy files BY NAME, not the whole directory. secret_globs.txt
        # matches its own '*secret*' pattern -- the gate's first self-inflicted false
        # positive -- but exempting all of security/ also exempted the one file in there
        # that must never be tracked, on the reasoning that .gitignore already covered it.
        # Relying on a single layer is exactly what this project does not do elsewhere,
        # and the gap was demonstrated: an empty forbidden_strings.txt committed cleanly.
        if r in POLICY_FILES:
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
        ("scripts/gen_gitleaks_config.py", ".gitleaks.toml"),
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
        if not path.startswith(("src/", "scripts/", ".github/", ".claude/")):
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


# How many commits may touch a document's owned code before the document itself is
# suspect. Not a calendar: a repository nobody touched for six months needs no audit,
# and one that changed forty times last week needs one regardless of the date.
AUDIT_PRESSURE_THRESHOLD = 12
AUDIT_STALE_COMMITS = 60


def _pathspec(globs: list[str]) -> list[str]:
    """Manifest globs to git pathspecs.

    `a/b/**` means "everything under a/b", which git expresses as the directory itself.
    Passing the literal `**` would match nothing and the check would silently measure
    zero pressure -- another check that reports success while looking at nothing.
    """
    out = []
    for g in globs:
        out.append(g[:-2] if g.endswith("/**") else g)
    return out


def check_audit_pressure() -> list[Finding]:
    """Tell you when a documentation audit is due, from evidence rather than a schedule.

    A scheduled audit fires whether or not anything changed, needs an API key, and gets
    ignored once it has cried wolf twice. This measures the thing that actually makes
    documentation wrong: its owned code moving while it did not.

    Warns only. The judgement it prompts -- is this still true, is it in the right
    document -- is what the docs-audit agent is for, run locally on demand.
    """
    manifest = load_manifest()
    if manifest is None:
        return [Finding(SKIP, "audit-due", f"{MANIFEST.name} not present.")]
    if not run("git", "rev-parse", "--verify", "HEAD"):
        return [Finding(SKIP, "audit-due", "no commits yet.")]

    out = []
    for doc, meta in manifest["docs"].items():
        owns = meta.get("owns", [])
        # Generated documents cannot drift: their freshness check already covers them.
        if not owns or meta.get("generated") or not (ROOT / doc).exists():
            continue
        doc_last = run("git", "log", "-1", "--format=%H", "--", doc)
        if not doc_last:
            continue
        since = run("git", "log", "--format=%H", f"{doc_last}..HEAD", "--",
                    *_pathspec(owns))
        n = len([x for x in since.splitlines() if x])
        if n >= AUDIT_PRESSURE_THRESHOLD:
            out.append(Finding(
                WARN, "audit-due",
                f"{doc} has not changed in {n} commits that touched the code it owns. "
                f"Not necessarily wrong, but worth a look: run the docs-audit agent."))

    audits = sorted((ROOT / "docs" / "audits").glob("*.md")) if (ROOT / "docs" / "audits").is_dir() else []
    total = len(run("git", "log", "--format=%H").splitlines())
    if audits:
        last = run("git", "log", "-1", "--format=%H", "--", f"docs/audits/{audits[-1].name}")
        n = len(run("git", "log", "--format=%H", f"{last}..HEAD").splitlines()) if last else total
    else:
        n = total
    if n >= AUDIT_STALE_COMMITS:
        out.append(Finding(
            WARN, "audit-due",
            f"{n} commits since the last recorded audit in docs/audits/. Run the "
            f"docs-audit agent and commit its findings there, which resets this."))
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
    "never-track": check_never_tracked,
    "commit-message": check_commit_message,
    "generated-doc": check_generated_docs,
    "budget": check_budgets,
    "adr": check_adr_format,
    "owning-doc": check_ownership,
    "orphan-doc": check_orphan_docs,
    "split-dodge": check_split_dodge,
    "manifest": check_manifest_docs_exist,
    "audit-due": check_audit_pressure,
    "public-text": check_pr_text,
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
    ap.add_argument("--pr-event", metavar="PATH", default=None,
                    help="Actions event payload; scans the pull request title and body, a public surface no hook can see.")
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
        if name in ("identity", "commit-message"):
            findings += fn(args.mode, args.diff_range)
        elif name == "public-text":
            findings += fn(args.pr_event)
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
