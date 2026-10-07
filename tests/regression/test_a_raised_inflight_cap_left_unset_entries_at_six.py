"""An entry with no `concurrency` stayed at 6 when the operator raised the global cap.

`docs/MODELS.md` says an unset `concurrency` "defaults to the global in-flight cap". The
registry took it from a literal copy of `max_inflight_seqs`'s *default* instead, so an
operator who raised `DELEGATE_MAX_INFLIGHT_SEQS` above 6 left every such endpoint capped
at 6 by its own limit: the bind the default exists to prevent, one level down. The
existing test only ever loaded the default config, where the two numbers agree.
"""

from __future__ import annotations

import dataclasses

from claude_delegate_local import config, registry

BARE = '[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\ndefault=true\n'


def _load(tmp_path, body: str, cap: int):
    path = tmp_path / "models.toml"
    path.write_text(body, encoding="utf-8")
    cfg = config.load({
        "DELEGATE_WORKSPACE_ROOTS": str(tmp_path),
        "DELEGATE_MODELS_FILE": str(path),
        "DELEGATE_MAX_INFLIGHT_SEQS": str(cap),
    })
    return registry.load(cfg).resolve(None), cfg


def _shipped_cap() -> int:
    return next(f.default for f in dataclasses.fields(config.Config)
                if f.name == "max_inflight_seqs")


def test_an_unset_concurrency_follows_a_raised_global_cap(tmp_path):
    raised = _shipped_cap() + 2
    entry, cfg = _load(tmp_path, BARE, raised)

    # Otherwise the assertion below holds whichever number the registry reads.
    assert cfg.max_inflight_seqs == raised != _shipped_cap()
    assert entry.concurrency == cfg.max_inflight_seqs, (
        f"the operator raised the global cap to {cfg.max_inflight_seqs}, but an entry that "
        f"set no concurrency kept {entry.concurrency}, so that endpoint admits fewer than "
        "the server would")


def test_a_set_concurrency_is_still_the_entry_own(tmp_path):
    """The fallback must not override a number the operator wrote."""
    entry, _ = _load(tmp_path, BARE + "concurrency = 2\n", _shipped_cap() + 2)

    assert entry.concurrency == 2
