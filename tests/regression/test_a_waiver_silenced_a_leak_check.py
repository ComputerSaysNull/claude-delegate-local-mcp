"""A `Docs-Gate-Skip` trailer waived a check by name, leak checks included.

`waivers()` collected every name its regex matched and `main()` dropped each BLOCK whose
check appeared in that set, so a single trailer silenced a host-identifier or secret-path
block for the whole branch -- and a pushed leak cannot be undone. A misspelled name waived
nothing and said nothing, which is the same silence from the other direction.

The tests run `main()` itself, with `_run_checks` stubbed so that no git is needed: the
verdict, the filtering and the reporting are the gate's own, not a copy of them. The
trailers are read from a real message file by the real parser, because `commit-msg` mode
is the one stage where `waivers()` does not shell out.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "scripts"))

import docs_gate  # noqa: E402  -- needs the path line above


def _gate(monkeypatch, tmp_path, message: str, findings: list[docs_gate.Finding],
          capsys) -> tuple[int, str]:
    """Run the gate over `findings`, with the trailers taken from `message`."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(message, encoding="utf-8")
    monkeypatch.setattr(docs_gate, "_run_checks", lambda args: ([], list(findings)))
    monkeypatch.setattr(sys, "argv", [
        "docs_gate.py", "--mode", "commit-msg", "--message-file", str(msg)])
    code = docs_gate.main()
    return code, capsys.readouterr().out


def _leak() -> docs_gate.Finding:
    return docs_gate.Finding(
        docs_gate.BLOCK, "host-identifier",
        "docs/notes.md: a host address reached a committed file")


def _owning_doc() -> docs_gate.Finding:
    return docs_gate.Finding(
        docs_gate.BLOCK, "owning-doc", "src/thing.py has no owning document")


def test_a_host_identifier_waiver_does_not_waive_the_leak(monkeypatch, tmp_path, capsys):
    code, out = _gate(
        monkeypatch, tmp_path,
        "fix: tidy the notes\n\n"
        "Docs-Gate-Skip: host-identifier -- it is only in a comment\n",
        [_leak()], capsys)

    assert code == 1, f"the run passed with a leak in it:\n{out}"
    assert "cannot be waived" in out, out
    assert "pushed leak" in out, out
    assert "WAIVED [host-identifier]" not in out, out


def test_an_owning_doc_waiver_still_waives_owning_doc(monkeypatch, tmp_path, capsys):
    code, out = _gate(
        monkeypatch, tmp_path,
        "fix: rename a module\n\n"
        "Docs-Gate-Skip: owning-doc -- pure rename, no behaviour change\n",
        [_owning_doc()], capsys)

    assert code == 0, out
    assert "WAIVED [owning-doc]" in out, out
    assert "reason given: pure rename, no behaviour change" in out, out


def test_an_unknown_check_name_is_reported_and_waives_nothing(monkeypatch, tmp_path,
                                                             capsys):
    code, out = _gate(
        monkeypatch, tmp_path,
        "fix: a thing\n\nDocs-Gate-Skip: owning-docs -- a typo\n",
        [_owning_doc()], capsys)

    assert code == 1, f"a misspelled name waived the check:\n{out}"
    assert "owning-docs" in out, out
    assert "unknown" in out.lower(), out
    assert "WAIVED [owning-docs]" not in out, out


def test_changed_files_is_a_known_name():
    """`changed-files` reports a git failure rather than standing for a check, but a
    waiver naming it is a waiver of something real, not a typo."""
    parsed = docs_gate.parse_waivers(
        "chore: mid-rebase\n\nDocs-Gate-Skip: changed-files -- git was unavailable\n")
    assert parsed.accepted.get("changed-files") == "git was unavailable"
    assert parsed.unknown == {}


def test_every_leak_check_is_refused():
    for name in ("identity", "email-content", "commit-message", "host-identifier",
                 "public-text", "secret-path", "never-track"):
        parsed = docs_gate.parse_waivers(f"chore: x\n\nDocs-Gate-Skip: {name} -- please\n")
        assert name in docs_gate.UNWAIVABLE_CHECKS, f"{name} is waivable"
        assert name in parsed.refused, f"{name} was accepted"
        assert name not in parsed.accepted
