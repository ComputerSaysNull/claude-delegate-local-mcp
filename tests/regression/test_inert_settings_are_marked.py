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
    """The positive case: kv_token_budget is declared and no module reads it yet.

    The specimen was sandbox_enabled until M5 deleted that field outright, then agents_dir
    until M6's agents.py started reading it. Any field for an unbuilt subsystem does here --
    kv_token_budget waits on M7's admission control -- and when the last of them goes live
    this test needs a new one rather than a weakened assertion. A version of it that quietly
    stopped naming a real field would pass forever.

    Note what replacing the specimen is *not*: an accommodation. Each time, the field went
    live because the subsystem was built, which is the marker doing its job. The assertion
    is rewritten to point at something still unbuilt, never loosened to keep passing.
    """
    unread = gen._unread_fields()
    assert "kv_token_budget" in unread


def test_a_setting_the_server_reads_is_not_reported_as_inert():
    """The negative case. Without this, marking everything would pass the test above."""
    unread = gen._unread_fields()
    assert "models_file" not in unread
    assert "workspace_roots" not in unread


def test_the_scan_does_not_mark_every_setting():
    """A scan that read nothing would mark every field and still render a believable file."""
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
    (fake / "uses_it.py").write_text("x = cfg.agents_dir\n", encoding="utf-8")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert "agents_dir" not in gen._unread_fields()


def _fake_tree(tmp_path, *, config_src: str, caller_src: str) -> None:
    fake = tmp_path / "src" / "claude_delegate_local"
    fake.mkdir(parents=True)
    (fake / "config.py").write_text(config_src, encoding="utf-8")
    (fake / "caller.py").write_text(caller_src, encoding="utf-8")


ACCESSOR_CONFIG = """
class Config:
    def effective_workdir_roots(self):
        return self.workdir_roots or self.workspace_roots
"""

CHAINED_CONFIG = """
class Config:
    def outer(self):
        return self.inner()

    def inner(self):
        return self.buried
"""


def test_a_field_read_only_through_a_live_accessor_is_not_marked(tmp_path, monkeypatch):
    """`workdir_roots` is named by nothing outside config.py and is still live.

    `paths.py` calls `effective_workdir_roots()`, and that property is where the "empty
    means reuse workspace_roots" fallback lives. Inlining the fallback at the call site to
    please a scanner would put a config default outside `config.py`, which is the one thing
    this project does not do -- so the scan follows the accessor instead.
    """
    _fake_tree(tmp_path, config_src=ACCESSOR_CONFIG,
               caller_src="y = cfg.effective_workdir_roots\n")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen.config, "describe", lambda: [{"field": "workdir_roots"}])
    assert gen._unread_fields() == set()


def test_an_accessor_nobody_calls_cannot_launder_a_dead_field(tmp_path, monkeypatch):
    """The negative half, and the one that makes the branch above worth having.

    Without it, any field touched by any property in config.py would read as live whether or
    not a caller existed -- which is under-marking, the direction this scan's own docstring
    calls the dangerous one. The accessor here is identical; nothing calls it, and that is
    the only difference.
    """
    _fake_tree(tmp_path, config_src=ACCESSOR_CONFIG, caller_src="y = something_else()\n")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen.config, "describe", lambda: [{"field": "workdir_roots"}])
    assert gen._unread_fields() == {"workdir_roots"}


def test_the_accessor_hop_does_not_chain(tmp_path, monkeypatch):
    """One level, deliberately. A property reached only from another property is not a use:
    chaining would let a dead field ride an arbitrarily long path back to looking live."""
    _fake_tree(tmp_path, config_src=CHAINED_CONFIG, caller_src="y = cfg.outer()\n")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen.config, "describe", lambda: [{"field": "buried"}])
    assert gen._unread_fields() == {"buried"}

