"""The inert marker in the generated config reference must be able to appear and vanish.

The second audit of 2026-08-27 found eighteen settings for unbuilt subsystems rendered
identically to settings that work, so a third of the reference read as live knobs. The fix
computes the marker by scanning the source rather than keeping a list, which means the
check has exactly the failure mode this project keeps finding: it could silently mark
nothing, or silently mark everything, and the generated file would still look plausible.

Both directions are asserted here. Named after the bug, per the project's convention.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import gen_config_docs as gen  # noqa: E402
from claude_delegate_local import config  # noqa: E402


def test_a_setting_nothing_reads_is_reported_as_inert():
    """The positive case: sandbox_enabled is declared and no module reads it yet."""
    unread = gen._unread_fields()
    assert "sandbox_enabled" in unread


def test_a_setting_the_server_reads_is_not_reported_as_inert():
    """The negative case. Without this, marking everything would pass the test above."""
    unread = gen._unread_fields()
    assert "models_file" not in unread
    assert "workspace_roots" not in unread


def test_the_scan_does_not_mark_every_setting():
    """A scan that read nothing would mark all 42 and still render a believable file."""
    unread = gen._unread_fields()
    total = len(config.describe())
    assert 0 < len(unread) < total


def test_the_marker_reaches_the_rendered_table():
    """The scan could be correct and the renderer still drop it on the floor."""
    block = gen.render()
    assert "**Inert.**" in block
    assert "of them inert" in block


def test_a_field_becomes_live_the_moment_source_mentions_it(tmp_path, monkeypatch):
    """The marker must clear itself, or it becomes a stale list by another name."""
    fake = tmp_path / "src" / "claude_delegate_local"
    fake.mkdir(parents=True)
    (fake / "config.py").write_text("# ignored by the scan\n", encoding="utf-8")
    (fake / "uses_it.py").write_text("x = cfg.sandbox_enabled\n", encoding="utf-8")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert "sandbox_enabled" not in gen._unread_fields()
