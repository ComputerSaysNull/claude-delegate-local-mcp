"""An unreadable denylist hit at tool declaration reached the caller without its status.

Declaring `search_files` reads the secret denylist, so `declared_tools` raises
`PathPolicyError` when the file is missing. Everywhere else that fault is caught and sent
as `STATUS_MISCONFIGURED: ...`, the status telling a caller the operator's configuration
is at fault rather than its own call. Raised at declaration, inside the dispatch, it fell
through the generic handler and reached the MCP layer unconverted, so the same fault read
two ways depending on which step met it first.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from test_server import cfg, chat_handler, refusal

from claude_delegate_local import server


def _missing(tmp_path) -> str:
    return str(tmp_path / "missing_secret_globs.txt")


def test_the_declaration_fault_carries_the_misconfigured_status(tmp_path):
    message = refusal(
        chat_handler(content="fine"),
        config=cfg(secret_globs_file=_missing(tmp_path)),
        task="x", allowed_tools=["search_files"],
    )
    assert "Layer 3 cannot run" in message, message
    assert server.STATUS_MISCONFIGURED in message, message


def test_the_same_fault_met_by_files_already_carried_it(tmp_path):
    """Control: the prefetch path's conversion is the behaviour the declaration now matches.

    If this ever lost the status too, the test above would be matching nothing.
    """
    target = tmp_path / "a.txt"
    target.write_text("a\n", encoding="utf-8")
    message = refusal(
        chat_handler(content="fine"),
        config=cfg(secret_globs_file=_missing(tmp_path), workspace_roots=(str(tmp_path),)),
        task="x", files=[str(target)],
    )
    assert "Layer 3 cannot run" in message, message
    assert server.STATUS_MISCONFIGURED in message, message
