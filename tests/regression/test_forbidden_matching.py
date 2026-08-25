"""Matching semantics for the local forbidden-strings list, pinned by test.

These were questions before they were tests: is matching case-sensitive, are substrings
caught, what happens to an entry containing a space. Answering them by reading the code
is how you end up confidently wrong, so each answer is asserted here and the file that
holds the literals documents the same rules.

Two gaps were found while pinning them down, both of which made a documented protection
weaker than it claimed:

  - The content scan used an allowlist of "text" extensions, so a literal in a .rst, .ts
    or .sql file passed silently. Fail-open, on a security check.
  - Commit messages were never scanned, although CLAUDE.md said they were. A message is
    the easiest place to leak a hostname, because you are describing the thing you were
    just debugging.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

LITERALS = ["host-alpha", "MixedCaseBox", "Ada Lovelace"]

# Fixture values are assembled at runtime so this source file contains no string that
# the gate would match. The tests still feed the complete value to the gate, so they
# exercise the real pattern -- but a test proving a secret-scanner works must not itself
# trip the secret scanner, and exempting the file would be a hole rather than a fix.
PRIVATE_ADDR = "192.168." + "1.50"
INTERNAL_HOST = "example" + ".internal"
OUTSIDE_EMAIL = "nobody@" + "a-real-domain.com"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    shutil.copy(ROOT / "scripts" / "docs_ownership.toml", r / "scripts" / "docs_ownership.toml")
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n"),
        ("secret_globs.txt", ".env\n"),
        ("content_safe_emails.txt", "t@example.com\n"),
        ("forbidden_strings.txt", "# local\n" + "\n".join(LITERALS) + "\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    return r


def probe(repo: Path, content: str, filename: str = "docs/p.md") -> bool:
    """True when the gate blocks on this file's content."""
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<!-- BUDGET: 99 -->\n" + content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run([sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
                          cwd=repo, capture_output=True, text=True)
    return any("host-identifier" in ln and Path(filename).name in ln
               for ln in proc.stdout.splitlines())


# ------------------------------------------------------------------ case and substrings

def test_matching_is_case_insensitive(repo: Path):
    assert probe(repo, "the box is HOST-ALPHA")
    assert probe(repo, "the box is Host-Alpha")


def test_a_lowercase_probe_matches_a_mixed_case_entry(repo: Path):
    assert probe(repo, "connect to mixedcasebox now")


def test_substrings_are_caught_so_a_longer_name_containing_one_is_too(repo: Path):
    """The short hostname is a substring of the fully qualified name, so listing the
    short form covers both. Listing both is harmless and clearer."""
    assert probe(repo, f"the box is host-alpha.{INTERNAL_HOST}")


def test_a_literal_with_no_separators_around_it_still_matches(repo: Path):
    assert probe(repo, "prefixhost-alphasuffix")


def test_unrelated_text_passes(repo: Path):
    assert not probe(repo, "nothing sensitive in this line at all")


# ------------------------------------------------------------------- multi-word entries

def test_a_multi_word_entry_matches_the_whole_phrase(repo: Path):
    assert probe(repo, "written by Ada Lovelace in 1843")


def test_a_multi_word_entry_is_caught_when_split_across_lines(repo: Path):
    """A name gets wrapped by an editor far more often than a hostname does, so the
    line-by-line pass is not enough on its own."""
    assert probe(repo, "written by Ada\nLovelace in 1843")


def test_a_multi_word_entry_is_caught_despite_odd_spacing(repo: Path):
    assert probe(repo, "written by Ada  Lovelace in 1843")


def test_single_words_of_a_multi_word_entry_do_NOT_match(repo: Path):
    """Deliberate, and the reason the list documents it.

    Matching each word separately would fire on ordinary prose -- plenty of surnames are
    also ordinary English words -- and a check that cries wolf gets ignored, which is
    worse than one that is narrow. Use fictional names in examples; a real one written
    here to illustrate the point would be the leak the rule exists to prevent.
    """
    assert not probe(repo, "written by Ada in 1843")
    assert not probe(repo, "the Lovelace algorithm is fast")


def test_a_reversed_multi_word_entry_does_not_match(repo: Path):
    """Documented limitation rather than a bug. "Lovelace, Ada" is a real way a name
    appears; list that form too if it matters."""
    assert not probe(repo, "written by Lovelace Ada")


# --------------------------------------------------------------- file type coverage

@pytest.mark.parametrize("ext", ["md", "rst", "txt", "ts", "js", "sql", "go", "rs",
                                 "html", "css", "json", "yaml", "cfg", "no-extension"])
def test_every_text_file_type_is_scanned(repo: Path, ext: str):
    """Regression: the scan used an allowlist of extensions, so a literal in a .rst or
    .sql file passed silently. Fail-open on a security check. It is a binary denylist
    now, so an unknown extension is scanned rather than skipped."""
    name = "docs/f" if ext == "no-extension" else f"docs/f.{ext}"
    assert probe(repo, "the box is host-alpha", filename=name), ext


def test_binary_files_are_skipped_without_error(repo: Path):
    (repo / "docs" / "blob.bin").write_bytes(bytes([0, 1, 2, 3]) * 100)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run([sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
                          cwd=repo, capture_output=True, text=True)
    assert "Traceback" not in proc.stdout + proc.stderr


# ------------------------------------------------------------------- commit messages

def message_blocked(repo: Path, message: str) -> bool:
    (repo / ".git" / "COMMIT_EDITMSG").write_text(message, encoding="utf-8")
    proc = subprocess.run([sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
                          cwd=repo, capture_output=True, text=True)
    return any("commit-message" in ln and "BLOCK" in ln for ln in proc.stdout.splitlines())


def test_a_forbidden_literal_in_the_commit_message_is_blocked(repo: Path):
    """CLAUDE.md claimed this for several commits before it was true."""
    assert message_blocked(repo, "fix: restart\n\ndebugged host-alpha this morning\n")


def test_a_private_address_in_the_commit_message_is_blocked(repo: Path):
    assert message_blocked(repo, f"fix: restart\n\nthe node at {PRIVATE_ADDR} was down\n")


def test_a_non_allowlisted_address_in_the_commit_message_is_blocked(repo: Path):
    assert message_blocked(repo, f"fix: restart\n\nreported by {OUTSIDE_EMAIL}\n")


def test_a_clean_commit_message_passes(repo: Path):
    assert not message_blocked(repo, "fix: restart the service\n\nIt had stopped.\n")


def test_comment_lines_in_the_template_are_ignored(repo: Path):
    """git's own template comments are not part of the message, and treating them as
    content would fire on branch names and file lists the user never wrote."""
    assert not message_blocked(
        repo, "fix: restart\n\n# On branch host-alpha\n# Changes to be committed:\n")


# ------------------------------------------------- word-boundary entries ("word:")

WORD_LITERALS = ["host-alpha", "word:Bell"]


@pytest.fixture
def word_repo(tmp_path: Path) -> Path:
    """A repo whose list uses a word-boundary entry for a name that is also a
    common word, which the default loose matching cannot serve."""
    r = tmp_path / "w"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    shutil.copy(ROOT / "scripts" / "docs_ownership.toml", r / "scripts" / "docs_ownership.toml")
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n"),
        ("secret_globs.txt", ".env\n"),
        ("content_safe_emails.txt", "t@example.com\n"),
        ("forbidden_strings.txt", "# local\n" + "\n".join(WORD_LITERALS) + "\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    return r


def test_word_entry_catches_the_capitalised_standalone_word(word_repo: Path):
    """The case that motivated this.

    A surname written on its own slipped past a list holding only the full name,
    because single words of a multi-word entry do not match. A loose entry could not
    fix it either, since the lowercase word is ordinary vocabulary."""
    assert probe(word_repo, "rung by Bell in the morning")


def test_word_entry_ignores_other_casings(word_repo: Path):
    assert not probe(word_repo, "ring the bell twice")
    assert not probe(word_repo, "the BELL character is 0x07")


def test_word_entry_ignores_the_word_inside_a_larger_token(word_repo: Path):
    """This is the whole point of word boundaries: the loose form would fire on all
    of these, and a check that fires on ordinary code gets switched off."""
    for text in ("Bellwether strategy", "doorBell handler", "Bell_Labs constant",
                 "the Bell-shaped curve is Bell-like"):
        blocked = probe(word_repo, text)
        if "Bell-" in text or "-Bell" in text:
            continue  # hyphen is a boundary; that form is correctly caught
        assert not blocked, text


def test_loose_entries_still_match_case_insensitively(word_repo: Path):
    """Adding the word: form must not change the default behaviour hostnames rely on."""
    assert probe(word_repo, "the box is HOST-ALPHA")
    assert probe(word_repo, "the box is host-alpha.example" + ".internal")


def test_word_entry_applies_to_commit_messages_too(word_repo: Path):
    assert message_blocked(word_repo, "fix: thing\n\nreported by Bell today\n")
    assert not message_blocked(word_repo, "fix: thing\n\nring the bell\n")
