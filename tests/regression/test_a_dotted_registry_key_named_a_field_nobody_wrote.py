"""A dotted registry key refused with a field the operator never wrote.

`[models.Name-V4.1-X]` is TOML for nested tables, so the registry saw an entry `Name-V4`
with an unknown field `1-X` and the server would not start. The refusal now says why.
"""

import pytest

from claude_delegate_local import config, registry

ENTRY = 'base_url = "http://h:1"\nserved_model_id = "s"\n'


def _load(tmp_path, body):
    path = tmp_path / "models.toml"
    path.write_text(body, encoding="utf-8")
    cfg = config.load({
        "DELEGATE_WORKSPACE_ROOTS": str(tmp_path),
        "DELEGATE_MODELS_FILE": str(path),
    })
    return registry.load(cfg)


def test_a_dotted_key_is_refused_with_the_quoting_fix(tmp_path):
    with pytest.raises(registry.RegistryError, match=r'quote the name.*"Name-V4\.1-X"'):
        _load(tmp_path, "[models.Name-V4.1-X]\n" + ENTRY)


def test_a_misspelt_scalar_field_gets_no_dot_hint(tmp_path):
    with pytest.raises(registry.RegistryError) as refused:
        _load(tmp_path, "[models.x]\n" + ENTRY + "tyop = 1\n")
    assert "quote" not in str(refused.value)
