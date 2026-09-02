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
EMAIL_EXEMPT_GLOBS = ("LICENSE", "NOTICE", "docs/audits/*", "docs/reviews/*",
                      "docs/specs/archive/*")

# Host-identifier shapes. These are patterns, never literals: a committed file listing
# the strings you do not want committed is itself the leak. The literals live in an
# untracked security/forbidden_strings.txt, mirrored as a CI secret.
HOST_PATTERNS: list[tuple[str, str]] = [
    (r"\b10(?:\.\d{1,3}){3}\b", "RFC1918 10.x address"),
    (r"\b192\.168(?:\.\d{1,3}){2}\b", "RFC1918 192.168.x address"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b", "RFC1918 172.16-31.x address"),
    (r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b",
     "CGNAT 100.64/10 address (overlay VPN range)"),
    # Private-DNS suffixes generally, not one vendor: wider protection, and
    # it does not advertise which kind of network sits behind it.
    (r"\b[a-z0-9-]+\.(ts\.net|internal|lan|home\.arpa|localdomain)\b",
     "private-network hostname"),
]

# `host:port` is only a leak when the host is a real name. Placeholders and loopback are
# how the examples are supposed to read.
HOSTPORT_RE = re.compile(r"\b([a-z][a-z0-9][a-z0-9.-]{1,40}):(\d{4,5})\b", re.IGNORECASE)
# Names only. HOSTPORT_RE starts with [a-z], so a numeric host never reaches this set --
# entries like "127.0.0.1" sat here looking load-bearing and were unreachable, one of them
# written twice. Loopback in an example is allowed because the pattern cannot match it,
# not because it is listed.
HOSTPORT_ALLOWED = {
    "localhost", "example.com", "example.org",
    "your-head-node", "head", "host", "hostname", "some-host",
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


# Written by the prepare-commit-msg hook when git says the message was reused from HEAD.
# That covers `git commit --amend` and `git commit -C HEAD`, which are indistinguishable
# from a hook's arguments -- both arrive as source="commit", sha="HEAD", and neither sets
# GIT_REFLOG_ACTION. Measured, not assumed.
REUSED_MSG_MARKER = ROOT / ".git" / "docs-gate-reused-message"


def message_reused_from_head() -> bool:
    """True if this commit's message came from HEAD. Consumes the marker.

    Consumed rather than merely read: a marker surviving an aborted commit would change
    how the *next* one is judged. The hook rewrites it on every commit anyway, so this is
    belt and braces on a check whose failure mode is a wrong verdict.
    """
    if not REUSED_MSG_MARKER.exists():
        return False
    REUSED_MSG_MARKER.unlink()
    return True


def files_against_previous_commit() -> list[str] | None:
    """The staged set diffed against HEAD~1, which is an amend's real parent.

    None when HEAD has no parent: amending a root commit has nothing to compare against,
    and inventing an empty tree here would quietly widen the set instead of saying so.
    """
    if not run("git", "rev-parse", "--verify", "--quiet", "HEAD~1"):
        return None
    out = run("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "HEAD~1")
    return [line for line in out.splitlines() if line]


def changed_files(mode: str, diff_range: str | None) -> list[str]:
    # commit-msg sees the same staged index as pre-commit: the hook runs before the commit
    # object exists, so the index still is the change under consideration. Falling through
    # to the CI branch here would diff against origin/main and, with no upstream, scan
    # every tracked file -- a whole-repo audit wearing the costume of a per-commit check.
    if mode in ("pre-commit", "commit-msg"):
        out = run("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        rng = diff_range or "origin/main...HEAD"
        out = run("git", "diff", "--name-only", "--diff-filter=ACMR", rng)
        if not out:  # first push, or no upstream yet: fall back to everything tracked
            out = run("git", "ls-files")
    return [line for line in out.splitlines() if line]


def scannable_files_with_skips() -> tuple[list[Path], list[Finding]]:
    """Every tracked file that is not provably binary, and what was left out.

    Binary detection is by content, not just extension: a NUL byte in the first 8 KiB.
    Extension alone is a guess, and guessing wrong here means skipping a file that holds
    the thing being looked for.

    The other two exclusions are *returned* rather than swallowed. A file over the byte
    cap and a file that cannot be opened were both skipped with a bare `continue`, so the
    email and host-identifier checks reported a clean pass over ground they had never
    covered -- a check that cannot fail, which CLAUDE.md names as worse than no check at
    all. Binary stays silent because excluding it is the intent, not a gap.
    """
    skips: list[Finding] = []
    files = []
    for r in run("git", "ls-files").splitlines():
        p = ROOT / r
        if not p.exists() or p.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            size = p.stat().st_size
            if size > MAX_SCAN_BYTES:
                skips.append(Finding(WARN, "scan-coverage",
                    f"{rel(p)} was not scanned for content: {size} bytes is over the "
                    f"{MAX_SCAN_BYTES}-byte cap. A host identifier or a non-allowlisted "
                    f"address inside it passes unseen."))
                continue
            with open(p, "rb") as fh:
                if b"\x00" in fh.read(8192):
                    continue
        except OSError as e:
            skips.append(Finding(WARN, "scan-coverage",
                f"{rel(p)} is tracked but could not be read "
                f"({e.strerror or e}), so it was not scanned for content."))
            continue
        files.append(p)
    return files, skips


def scannable_files() -> list[Path]:
    """The paths alone, for the scanners that read files rather than report coverage."""
    return scannable_files_with_skips()[0]


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
            if ce not in (ae, "noreply" + "@github.com"):
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
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    out.append(Finding(BLOCK, "host-identifier",
                                       f"{r}:{i} looks like a {label}: {m.group(0)!r}. "
                                       f"Hosts are configuration, never literals."))
            for hit in hostport_leaks(line):
                out.append(Finding(
                    BLOCK, "host-identifier",
                    f"{r}:{i} contains {hit!r}. A hostname is as identifying "
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


def check_commit_message(mode: str, diff_range: str | None,
                         message_file: str | None = None) -> list[Finding]:
    """Scan the commit message itself.

    CLAUDE.md stated the gate blocked host literals in "code, docs, tests or a commit
    message". It did not: the only place the message was read was to parse waiver
    trailers. A document promising protection that does not exist is worse than no
    promise, because it is relied upon. A message is also the easiest place to leak a
    hostname -- you are describing the thing you were just debugging.
    """
    if mode == "commit-msg":
        if not message_file:
            return [Finding(BLOCK, "commit-message",
                            "commit-msg mode requires --message-file.")]
        f = Path(message_file)
        raw = f.read_text(encoding="utf-8", errors="replace") if f.exists() else None
        texts = [("pending commit", raw)] if raw is not None else []
    elif mode == "pre-commit":
        # Nothing to read. git writes .git/COMMIT_EDITMSG *after* the pre-commit hook
        # returns, so reading it here inspects the PREVIOUS commit's message -- the check
        # passed on text nobody was proposing to commit. The real scan runs at commit-msg.
        return [Finding(SKIP, "commit-message",
                        "the message does not exist yet at pre-commit; scanned by the "
                        "commit-msg hook, which is handed the real file.")]
    else:
        rng = diff_range or "origin/main...HEAD"
        raw = run("git", "log", "--no-merges", "--format=%h%x1f%B%x1e", rng)
        texts = []
        for chunk in raw.split(chr(30)):
            if chr(31) in chunk:
                sha, body = chunk.split(chr(31), 1)
                texts.append((sha.strip(), body))

    out = []
    for where, text in texts:
        # A comment line in a commit template is not part of the message.
        body = chr(10).join(ln for ln in text.splitlines() if not ln.startswith("#"))
        # The second surface. A squash of a single-commit branch takes its subject from
        # here rather than from the pull request title, so checking only the title would
        # leave exactly half of what reaches main unchecked -- which is how "M1: ..." and
        # "M3: ..." both landed while every title around them was well formed.
        subject = next((ln for ln in body.splitlines() if ln.strip()), "")
        finding = conventional_subject_finding(f"{where}: the subject", subject)
        if finding:
            out.append(finding)
        # The same scan the pull request title and body get. A commit message is no less
        # public once it is pushed, and it used to get a near-copy of this that had
        # drifted -- the copy never learned about host:port.
        out += scan_text(f"{where}: the message", body, "commit-message")
    return out


def hostport_leaks(text: str) -> list[str]:
    """Every `host:port` in `text` whose host is a real name rather than a placeholder.

    One predicate, used by the file scan and by `scan_text`, so the surfaces cannot
    disagree about what a leak is. They did disagree, for long enough to matter: this ran
    against tracked files only, and the same string was refused in one place and accepted
    in another.
    """
    out = []
    for m in HOSTPORT_RE.finditer(text):
        host = m.group(1).lower()
        if host not in HOSTPORT_ALLOWED and not host.startswith("your-"):
            out.append(m.group(0))
    return out


def scan_text(label: str, text: str, check: str = "public-text") -> list[Finding]:
    """Run the identifier checks over arbitrary text.

    The single implementation for the commit message and for a pull request title and
    body. It was documented as that before it was one: another caller carried a second
    copy that had drifted and no longer ran everything this does. `check` and `label` are
    the only things that differed, so they are the only things parameterised.

    Extend this rather than adding a check beside it. Two copies cannot be kept in step by
    intention alone -- that is what the previous pair proved, over months, unnoticed.

    A pull request body is a public surface written outside git entirely, so no hook and
    no file check can see it -- which is exactly how a specimen got published once despite
    every other check passing.
    """
    loose, words = split_literals(
        load_lines(ROOT / "security" / "forbidden_strings.txt"))
    allowed = set(load_lines(AUTHOR_ALLOWLIST_FILE))
    flat = " ".join(text.split()).lower()
    out = []
    for lit in loose:
        if " ".join(lit.split()).lower() in flat:
            out.append(Finding(BLOCK, check,
                               f"{label} contains an entry from the local "
                               f"forbidden_strings list."))
            break
    for lit in words:
        if re.search(_word_pattern(lit), text):
            out.append(Finding(BLOCK, check,
                               f"{label} contains a word-boundary entry from the local "
                               f"forbidden_strings list."))
            break
    for pat, lbl in HOST_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            out.append(Finding(BLOCK, check,
                               f"{label} contains what looks like a {lbl}: "
                               f"{m.group(0)!r}."))
    for hit in hostport_leaks(text):
        out.append(Finding(BLOCK, check,
                           f"{label} contains {hit!r}. A hostname is as identifying as "
                           f"an address. Use a placeholder."))
    for m in EMAIL_RE.finditer(text):
        if m.group(0) not in allowed and not is_content_safe(m.group(0)):
            out.append(Finding(BLOCK, check,
                               f"{label} contains {m.group(0)!r}, which is not an "
                               f"allowlisted address."))
    return out


# The authority for the commit/title prefix. CONTRIBUTING.md names the same six in prose
# for a human to read; `test_contributing_lists_exactly_the_gated_types` asserts the two
# agree, so the second copy cannot drift quietly the way an unchecked one would.
CONVENTIONAL_TYPES = ("feat", "fix", "docs", "test", "refactor", "chore")
CONVENTIONAL_SUBJECT = re.compile(
    r"^(?:" + "|".join(CONVENTIONAL_TYPES) + r")(?:\([^)]+\))?!?: \S")
# A merge or a revert is generated by git, not written by anyone, so it is not held to a
# convention nobody had the chance to apply.
GENERATED_SUBJECT = re.compile(r"^(?:Merge |Revert )")


def conventional_subject_finding(where: str, subject: str) -> Finding | None:
    """One place decides what a well-formed subject is, for both surfaces it is checked on."""
    if not subject.strip() or GENERATED_SUBJECT.match(subject):
        return None
    if CONVENTIONAL_SUBJECT.match(subject):
        return None
    return Finding(
        BLOCK, "conventional-subject",
        f"{where} is not a Conventional Commit: {subject[:60]!r}. Expected one of "
        f"{', '.join(CONVENTIONAL_TYPES)} followed by ': '. The milestone belongs in the "
        f"body or in PLAN.md, not in the subject -- 'M1: ...' reached main twice before "
        f"this check existed.")


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
    title = pr.get("title") or ""
    out += scan_text("the pull request title", title)
    out += scan_text("the pull request body", pr.get("body") or "")
    # The title is the *decisive* surface: on a squash merge of a multi-commit branch it
    # becomes the subject on main. Unlike a leaked literal, a malformed title is repaired
    # by editing the pull request, so this blocks rather than merely warning.
    finding = conventional_subject_finding("the pull request title", title)
    if finding:
        out.append(finding)
    # A clean result reports nothing, as every other check does. Reporting it as SKIP
    # would collapse the one distinction SKIP exists to preserve: "checked and found
    # nothing" against "could not check". That ambiguity is how a no-op check comes to
    # be trusted.
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


def generator_targets() -> list[tuple[str, str]]:
    """The (script, rendered document) pairs. One copy, read by two checks."""
    return [
        ("scripts/gen_config_docs.py", "docs/CONFIGURATION.md"),
        ("scripts/gen_gitleaks_config.py", ".gitleaks.toml"),
        ("scripts/gen_agents_docs.py", "CONTRIBUTING.md"),
        ("scripts/gen_tools_docs.py", "docs/TOOLS.md"),
    ]


def check_generated_docs() -> list[Finding]:
    out = []
    for script, target in generator_targets():
        if not (ROOT / script).exists():
            out.append(Finding(SKIP, "generated-doc", f"{script} not written yet."))
            continue
        proc = subprocess.run([sys.executable, script, "--check"],
                              cwd=ROOT, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            first = (proc.stdout or proc.stderr).strip().splitlines()
            out.append(Finding(BLOCK, "generated-doc",
                               f"{target} is stale. Run: python {script}. "
                               f"({first[0] if first else 'no output'})"))
    return out


def check_generated_docs_are_all_checked() -> list[Finding]:
    """Every document the manifest calls generated must be in the freshness list.

    That list is hand-written, so adding a generator and forgetting the tuple leaves the
    new document with no freshness check at all -- passing for the reason that nothing
    looked. The manifest already records which documents are generated, so the two can be
    compared instead of both being trusted.

    One-way on purpose. The freshness list also covers .gitleaks.toml and
    CONTRIBUTING.md, which are generated in part and own no code, so they carry no
    `generated` flag; requiring the sets to match would flag those forever.
    """
    manifest = load_manifest()
    if manifest is None:
        return [Finding(SKIP, "generated-doc", f"{MANIFEST.name} not present.")]
    checked = {target for _, target in generator_targets()}
    out = []
    for doc, meta in manifest["docs"].items():
        if meta.get("generated") and doc not in checked:
            out.append(Finding(
                BLOCK, "generated-doc",
                f"{MANIFEST.name} marks {doc} as generated, but no generator in "
                f"check_generated_docs() renders it, so nothing checks it is current. Add "
                f"the (script, target) pair there in the same commit as the generator."))
    return out


def check_budgets() -> list[Finding]:
    """Budgets block, but the block never means delete. See ADR-0003.

    Two kinds, because one rule does not fit both document classes (ADR-0022):

    `BUDGET: n`           total lines. For MUTABLE documents, where exceeding the cap is a
                          real prompt to ask whether everything still earns its place.
    `BUDGET-PER-ENTRY: n` longest `## ` section. For APPEND-ONLY documents, where history
                          does not stop earning its place, so a total cap could only ever
                          be raised. Capping each entry keeps them terse instead.
    There is deliberately no total-size instrument for append-only documents. One was
    tried -- `ARCHIVE-AT: n`, warning when the file passed a line count -- and it warned
    without a remedy anyone could apply: the procedure it pointed at split by year, and a
    document whose entries are all one year has no older year to move. It fired on every
    commit until it stopped being read, which is the failure a warning has. Archiving is
    now a judgement someone makes, not a threshold. (ADR-0033)
    """
    out = []
    seen = 0
    for p in sorted(ROOT.rglob("*.md")):
        r = rel(p)
        if (".git" in p.parts or "docs/audits" in r or "docs/reviews" in r
                or "archive" in p.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        total = len(text.splitlines())

        per_entry = re.search(r"<!--\s*BUDGET-PER-ENTRY:\s*(\d+)", text)
        if per_entry:
            seen += 1
            cap = int(per_entry.group(1))
            sections = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
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

        m = re.search(r"<!--\s*BUDGET:\s*(\d+)", text)
        if not m:
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


def check_adr_format(text: str | None = None) -> list[Finding]:
    """The ADR headings ARE the index, so their shape is load-bearing.

    `text` is injectable so a test can drive *this* function over synthetic headings
    instead of reimplementing it. The reimplementation is the trap: a copy of the rule in
    the test file passes whether or not the rule here still works, which is the same shape
    as the bugs this check was written for.
    """
    if text is None:
        p = ROOT / "DECISIONS.md"
        if not p.exists():
            return [Finding(SKIP, "adr", "DECISIONS.md not written yet.")]
        text = p.read_text(encoding="utf-8")
    heads = re.findall(r"^## (.+)$", text, re.MULTILINE)
    if not heads:
        return [Finding(BLOCK, "adr", "DECISIONS.md declares no ADR headings.")]
    # A decision can be overtaken by more than one later one, on different clauses:
    # ADR-0005 lost its portability claim to ADR-0031 and its tool count to ADR-0042.
    # Admitting only a single reference meant the heading could name just the newest, and
    # the earlier correction survived only in whatever prose happened to mention it.
    ref_list = r"ADR-\d{4}(?:(?:, | and )ADR-\d{4})*"
    pattern = re.compile(
        r"^(?:~~)?ADR-(\d{4}) — \d{4}-\d{2}-\d{2} — .+?(?:~~)? — "
        rf"(Accepted|Proposed|Rejected|Superseded by {ref_list}|"
        rf"Partially superseded by {ref_list})$"
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
        # Every reference, not the first. `re.search` stopped at one, so widening the
        # grammar above without widening this would leave the second and later targets
        # unvalidated -- a heading could point at ADR-9999 and pass.
        if (tail := re.search(r"[Ss]uperseded by (.+)$", h)) is not None:
            for target in re.findall(r"ADR-(\d{4})", tail.group(1)):
                if int(target) not in declared:
                    out.append(Finding(BLOCK, "adr",
                                       f"ADR-{m.group(1)} points at ADR-{target}, which "
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

# Lowered from 60 on 2026-08-27. Sixty was set when the repository had fourteen commits and
# read as "a long while"; at the rate this one actually moves it is months, and a whole
# milestone can land and go stale inside one interval. The first real audit found two wrong
# entries in the one document a milestone had just changed, at 26 commits -- less than half
# the old threshold, so the counter would not have asked. A number that only fires after the
# damage is a number that never fires.
AUDIT_STALE_COMMITS = 15


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

    # Ask git for the most recent commit touching docs/audits/, rather than picking a
    # filename and dating that. The previous version took the alphabetically last *.md,
    # which is only ever right while every file in the directory shares one naming scheme.
    # It did not: an upstream review landed here, sorted after every `YYYY-MM-DD-audit.md`
    # by virtue of starting with a letter, and reset this counter to zero -- silently, and
    # for a record that is not a documentation audit at all. Upstream reviews now live in
    # docs/reviews/. Asking git removes the dependency on naming entirely. (ADR-0025)
    audit_dir = ROOT / "docs" / "audits"
    has_audit = audit_dir.is_dir() and any(audit_dir.glob("*.md"))
    total = len(run("git", "log", "--format=%H").splitlines())
    last = run("git", "log", "-1", "--format=%H", "--", "docs/audits") if has_audit else ""
    n = len(run("git", "log", "--format=%H", f"{last}..HEAD").splitlines()) if last else total
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


def check_split_dodge() -> list[Finding]:
    """A new document must earn its existence, or it is a budget being evaded.

    Valid reasons to split: a different audience, different owned code, or reference
    material separated from narrative. "It got long" is a reason to raise a budget, not
    to create a file. See ADR-0003.

    Takes no changed-file list on purpose, and must not be given one. This check needs
    the files being ADDED, which `changed_files()` does not distinguish -- it reports
    that a path changed, not how. It previously accepted a `changed` argument and
    ignored it, which is worse than either: a signature describing a function this is
    not, and an invitation to "fix" the check by wiring the wrong list into it.
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


def check_scan_coverage() -> list[Finding]:
    """What the content scanners declined to look at, and why.

    A WARN rather than a BLOCK: a genuinely large tracked file is a cost decision, not a
    violation. What is not acceptable is it being invisible -- `check_emails_in_files` and
    `check_host_identifiers` both pass over whatever this skips, and a pass nobody knows is
    partial is the one that gets trusted. Silent today, because no tracked file is over the
    cap; it speaks the moment one is.
    """
    return scannable_files_with_skips()[1]


ENV_EXAMPLE = ROOT / ".env.example"
ENV_NAME_RE = re.compile(r"DELEGATE_[A-Z0-9_]+")


def check_env_example() -> list[Finding]:
    """Every DELEGATE_* name in .env.example must be a setting that exists.

    `.env.example` advertised `DELEGATE_SANDBOX_ENABLED` long after ADR-0034 deleted the
    field, describing it as "an explicit choice to run shell commands with no confinement"
    -- very nearly the words the ADR quoted while removing exactly that foot-gun. Nothing
    could have caught it, because to every other check the example file is prose.

    The allowed set is read from `docs/CONFIGURATION.md`, not from a list kept here and not
    by importing `config.py`. This job does not install the package, and a list here would
    be the same second copy that let the example drift from the code in the first place.
    That document is generated from the dataclass and `check_generated_docs` blocks while it
    is stale, so its names are `config.py`'s names by the time this runs.
    """
    if not ENV_EXAMPLE.exists():
        return [Finding(SKIP, "env-example", ".env.example is not present.")]
    reference = ROOT / "docs" / "CONFIGURATION.md"
    if not reference.exists():
        return [Finding(SKIP, "env-example",
                        "docs/CONFIGURATION.md is absent, so no name list can be derived.")]

    known = set(ENV_NAME_RE.findall(reference.read_text(encoding="utf-8")))
    if not known:
        return [Finding(BLOCK, "env-example",
                        "no DELEGATE_* names found in docs/CONFIGURATION.md, so this check "
                        "would pass whatever .env.example said. Refusing to be a check that "
                        "cannot fail.")]

    out = []
    for name in sorted(set(ENV_NAME_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))):
        if name not in known:
            out.append(Finding(BLOCK, "env-example",
                               f".env.example advertises {name}, which is not a setting "
                               f"docs/CONFIGURATION.md knows. Either it was renamed or it "
                               f"was deleted -- a removed knob left in a tracked example is "
                               f"still advice, and someone will follow it."))
    return out


CHECKS = {
    "identity": check_commit_identity,
    "email-content": check_emails_in_files,
    "host-identifier": check_host_identifiers,
    "scan-coverage": check_scan_coverage,
    "secret-path": check_secret_paths,
    "never-track": check_never_tracked,
    "env-example": check_env_example,
    "commit-message": check_commit_message,
    "generated-doc": check_generated_docs,
    "generated-coverage": check_generated_docs_are_all_checked,
    "budget": check_budgets,
    "adr": check_adr_format,
    "owning-doc": check_ownership,
    "orphan-doc": check_orphan_docs,
    "split-dodge": check_split_dodge,
    "manifest": check_manifest_docs_exist,
    "audit-due": check_audit_pressure,
    "public-text": check_pr_text,
}


def waivers(mode: str, message_file: str | None = None) -> dict[str, str]:
    """Parse `Docs-Gate-Skip: <check> -- <reason>` trailers.

    Waivers are echoed in the output on purpose. An escape hatch nobody can see becomes
    the default route; one that leaves a visible trail in every run does not.
    """
    if mode == "commit-msg":
        f = Path(message_file) if message_file else None
        text = f.read_text(encoding="utf-8", errors="replace") if f and f.exists() else ""
    elif mode == "pre-commit":
        # See check_commit_message: the message is not written yet. Reading COMMIT_EDITMSG
        # here would apply the PREVIOUS commit's waiver to this one -- an escape hatch
        # firing on a commit that never asked for it.
        text = ""
    else:
        text = run("git", "log", "--format=%B", "origin/main...HEAD")
    out = {}
    for m in re.finditer(r"^Docs-Gate-Skip:\s*([a-z-]+)\s*(?:--|—)\s*(.+)$", text, re.MULTILINE):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def amend_hint(mode: str) -> str:
    """Shown beside an owning-doc block, because the commonest cause is not a missing doc.

    `git commit --amend -m` is the one amend the gate cannot see. Git tells the
    prepare-commit-msg hook where a message came from, and only an amend that *reuses*
    the existing message says "commit"/"HEAD"; supplying a new one with `-m` or `-F` is
    reported exactly like an ordinary commit. Nothing else distinguishes them:
    GIT_REFLOG_ACTION is unset for both, and reading the parent's argv out of /proc --
    which does work under WSL -- is not available where these hooks actually run, because
    Git for Windows gives the hook a PPID of 1 with no readable entry. Both measured.

    So the remedy is a workflow one and belongs where someone hits the wall. `git commit
    --amend` with an editor is detected *and* lets the message change, which is the whole
    of what `--amend -m` was wanted for.
    """
    hint = ("if this is a `git commit --amend`, git only reports an amend to the hook when "
            "the message is reused, so `--amend -m` looks exactly like an ordinary commit "
            "and the commit is judged against HEAD rather than its real parent. Re-run it "
            "as `git commit --amend` and change the message in the editor -- that form is "
            "detected. Reach for Docs-Gate-Skip only once that has been tried.")
    if mode == "pre-commit":
        hint += (" Run by hand at pre-commit, no amend can be detected at all: the marker "
                 "is written by prepare-commit-msg, which has not run yet. The binding "
                 "check is the commit-msg hook.")
    return hint


def ownership_findings(changed: list[str], reused_message: bool, mode: str) -> list[Finding]:
    """Ownership, judged against the parent the resulting commit will actually have.

    A normal commit is the index against HEAD. An amend is the index against HEAD~1: the
    files already inside the commit being amended are part of what lands, but they are not
    in the index, so judging an amend against HEAD reports a complete commit as incomplete.
    That blocked real work and the documented workaround was to undo the commit and remake
    it, which is a lot of ceremony to answer a question the tool got wrong.

    It cannot be detected outright. `git commit --amend` and `git commit -C HEAD` reach a
    hook identically -- source="commit", sha="HEAD", GIT_REFLOG_ACTION unset -- and only
    the first has HEAD~1 as its parent. So both readings are evaluated, the strict one
    first, and a pass that depended on the amend reading is *announced* rather than taken
    quietly. An escape hatch nobody can see becomes the default route; this one leaves a
    line in every run that used it.
    """
    strict = check_ownership(changed)
    if not any(f.level == BLOCK for f in strict):
        return strict
    if not reused_message:
        return [*strict, Finding(WARN, "owning-doc", amend_hint(mode))] if mode in (
            "commit-msg", "pre-commit") else strict

    widened = files_against_previous_commit()
    if widened is None:
        return [*strict, Finding(
            WARN, "owning-doc",
            "this commit reuses HEAD's message but HEAD has no parent, so there is no "
            "amend reading to check. Judged against HEAD alone.")]

    relaxed = check_ownership(widened)
    if any(f.level == BLOCK for f in relaxed):
        return relaxed

    extra = sorted(set(widened) - set(changed))
    return [*relaxed, Finding(
        WARN, "owning-doc",
        "passed only when read as an amend. This commit reuses HEAD's message, so its "
        f"parent is HEAD~1 rather than HEAD, and {len(extra)} file(s) already inside the "
        f"commit being amended were counted: {', '.join(extra[:6])}"
        f"{' ...' if len(extra) > 6 else ''}. If this was `git commit -C HEAD` and not an "
        "amend, the owning document for the changed code is in the PREVIOUS commit, not "
        "this one.")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("pre-commit", "commit-msg", "ci"),
                    default="pre-commit")
    ap.add_argument("--message-file", metavar="PATH", default=None,
                    help="the commit message file, as git hands it to a commit-msg "
                         "hook. The only stage where the real message exists.")
    ap.add_argument("--diff", dest="diff_range", default=None)
    ap.add_argument("--pr-event", metavar="PATH", default=None,
                    help="Actions event payload; scans the pull request title and "
                         "body, a public surface no hook can see.")
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
                gen = (" (generated -- run its generator, do not hand-edit)"
                   if manifest["docs"][d].get("generated") else "")
                print(f"{d}{gen}")
            return 0
        if _matches(path, manifest.get("unowned", {}).get("paths", [])):
            print(f"{path} is declared unowned in {MANIFEST.name}")
            return 0
        print(f"{path} has no owning document and is not declared unowned -- "
              f"assign it in {MANIFEST.name}")
        return 1

    changed = changed_files(args.mode, args.diff_range)
    reused_message = message_reused_from_head() if args.mode == "commit-msg" else False
    waived = waivers(args.mode, args.message_file)

    findings: list[Finding] = []
    for name, fn in CHECKS.items():
        if name == "commit-message":
            findings += fn(args.mode, args.diff_range, args.message_file)
        elif name == "identity":
            findings += fn(args.mode, args.diff_range)
        elif name == "public-text":
            findings += fn(args.pr_event)
        elif name == "owning-doc":
            findings += ownership_findings(changed, reused_message, args.mode)
        else:
            findings += fn()

    blocks = [f for f in findings if f.level == BLOCK and f.check not in waived]
    waived_hits = [f for f in findings if f.level == BLOCK and f.check in waived]
    warns = [f for f in findings if f.level == WARN]
    skips = [f for f in findings if f.level == SKIP]

    # Says what was counted, not just how many. An amend stages only its increment, so
    # "1 changed file(s)" on a commit holding six is accurate and reads as a bug report;
    # naming the comparison is what makes the number legible. When the ownership check
    # widened to HEAD~1 it says so in its own finding, which is the only check that does.
    against = (args.diff_range or "origin/main...HEAD") if args.mode == "ci" else "HEAD"
    print(f"docs gate: mode={args.mode}, {len(changed)} file(s) changed against {against}")
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
