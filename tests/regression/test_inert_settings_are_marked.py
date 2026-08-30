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


def test_a_setting_nothing_reads_is_reported_as_inert(tmp_path, monkeypatch):
    """The positive case, against a constructed tree because the real one ran out.

    The specimen was sandbox_enabled until M5 deleted that field outright, then agents_dir
    until M6 read it, then kv_token_budget until M7's admission gate read that. M7 was the
    last unbuilt subsystem with settings, so there is no longer any real field to point at
    -- and the previous docstring's instruction, "a new one rather than a weakened
    assertion", has no candidate left to name.

    Constructing the subject is not the weakening it warned about. The assertion is the
    same one, at full strength, and it can now never go vacuous by having its subject
    built: `_fake_tree` gives it a field that is unread by construction. What the real
    tree is still asserted against is the opposite direction, below.
    """
    _fake_tree(
        tmp_path,
        config_src="class Config:\n    pass\n",
        caller_src="y = something_else()\n",
    )
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen.config, "describe", lambda: [{"field": "nobody_reads_this"}])
    assert gen._unread_fields() == {"nobody_reads_this"}


def test_nothing_in_the_real_tree_is_inert_today():
    """The state M7 left the project in, asserted rather than assumed.

    Every setting is now read by something. This is not a placeholder for the test above:
    it fails the day a field is added for a subsystem that does not exist yet, which is
    exactly when the marker has to start working again and someone should be looking at
    it. Without it, the vanished specimen would leave a hole rather than a check.
    """
    assert gen._unread_fields() == set()


def test_a_setting_the_server_reads_is_not_reported_as_inert():
    """The negative case. Without this, marking everything would pass the test above."""
    unread = gen._unread_fields()
    assert "models_file" not in unread
    assert "workspace_roots" not in unread


def test_the_scan_does_not_mark_every_setting():
    """A scan that read nothing would mark every field and still render a believable file.

    The lower bound this used to carry moved to the constructed tree above, where a
    non-empty result is guaranteed by construction rather than by whatever happens to be
    unbuilt this month.
    """
    unread = gen._unread_fields()
    total = len(config.describe())
    assert len(unread) < total


def test_the_marker_reaches_the_rendered_table(monkeypatch):
    """The scan could be correct and the renderer still drop it on the floor.

    Forced rather than observed, for the same reason as the positive case above: with
    nothing genuinely inert this would assert against an empty table and pass by
    describing a path that no longer runs.
    """
    monkeypatch.setattr(gen, "_unread_fields", lambda: {"kv_token_budget"})
    block = gen.render()
    assert "**Inert.**" in block
    assert "1 of them inert" in block


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

