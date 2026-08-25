"""The registry refuses malformed input rather than guessing.

Replacing prefix matching with explicit rows (ADR-0009) only pays if the rows are
validated. The ancestor's five prefix tables failed silently -- a model whose name lacked
an expected suffix quietly got the wrong token budget. Every refusal here names the fix.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import config, registry

GOOD = """
[models.deepseek]
base_url = "http://head:8888"
served_model_id = "deepseek-v4-flash-0731"
context_window = 1048576
default_effort = "low"
max_tokens_cap = 131072
concurrency = 5
default = true
"""


def build(tmp_path, body: str, **env):
    path = tmp_path / "models.toml"
    path.write_text(body, encoding="utf-8")
    cfg = config.load({
        "DELEGATE_WORKSPACE_ROOTS": str(tmp_path),
        "DELEGATE_MODELS_FILE": str(path),
        **env,
    })
    return registry.load(cfg), cfg


def test_resolves_the_default_entry(tmp_path):
    reg, _ = build(tmp_path, GOOD)
    assert reg.resolve(None).key == "deepseek"


def test_urls_are_derived_not_configured(tmp_path):
    reg, _ = build(tmp_path, GOOD)
    entry = reg.resolve(None)
    assert entry.chat_url == "http://head:8888/v1/chat/completions"
    assert entry.models_url == "http://head:8888/v1/models"


def test_health_probe_uses_v1_models_not_a_proxy_specific_path(tmp_path):
    """Regression: the ancestor probed /health/liveliness, which belongs to the proxy it
    used to sit behind. Bare vLLM does not serve it, so a healthy cluster looked down."""
    reg, _ = build(tmp_path, GOOD)
    assert "health" not in reg.resolve(None).models_url


def test_a_model_can_be_named_by_its_served_id_too(tmp_path):
    """A caller sees served_model_id in backend_status output, so accept it as a handle
    rather than making them learn two names for one thing."""
    reg, _ = build(tmp_path, GOOD)
    assert reg.resolve("deepseek-v4-flash-0731").key == "deepseek"


def test_per_model_cap_clamps_a_larger_request(tmp_path):
    reg, _ = build(tmp_path, GOOD)
    assert reg.resolve(None).cap_tokens(500_000) == 131_072


def test_effort_falls_back_to_the_global_default_when_unset(tmp_path):
    body = GOOD.replace('default_effort = "low"\n', "")
    reg, cfg = build(tmp_path, body)
    assert reg.resolve(None).effective_effort(cfg) == cfg.thinking_default


def test_unknown_model_names_the_registered_ones(tmp_path):
    reg, _ = build(tmp_path, GOOD)
    with pytest.raises(registry.RegistryError, match="Unknown model"):
        reg.resolve("gpt-4")


def test_there_is_no_name_pattern_fallback(tmp_path):
    """The whole point of ADR-0009: a name that merely resembles a registered one is not
    silently routed somewhere plausible."""
    reg, _ = build(tmp_path, GOOD)
    with pytest.raises(registry.RegistryError):
        reg.resolve("deepseek-v4-flash-9999")


# ------------------------------------------------------------------ malformed input


def test_unknown_field_is_refused_rather_than_ignored(tmp_path):
    """A typo in a registry key would otherwise cost you the setting in silence."""
    body = GOOD + '\n[models.x]\nbase_url="http://h:1"\nserved_model_id="s"\ntyop=1\n'
    with pytest.raises(registry.RegistryError, match="unknown field"):
        build(tmp_path, body)


def test_base_url_with_a_v1_suffix_is_refused_with_the_correction(tmp_path):
    body = '[models.a]\nbase_url="http://h:8888/v1"\nserved_model_id="s"\ndefault=true\n'
    with pytest.raises(registry.RegistryError, match="should not include the /v1"):
        build(tmp_path, body)


def test_base_url_must_have_a_scheme(tmp_path):
    body = '[models.a]\nbase_url="h:8888"\nserved_model_id="s"\ndefault=true\n'
    with pytest.raises(registry.RegistryError, match="http"):
        build(tmp_path, body)


def test_missing_required_field_is_named(tmp_path):
    body = '[models.a]\nbase_url="http://h:1"\ndefault=true\n'
    with pytest.raises(registry.RegistryError, match="served_model_id"):
        build(tmp_path, body)


def test_bad_effort_value_is_refused(tmp_path):
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\n'
            'default_effort="maximum"\ndefault=true\n')
    with pytest.raises(registry.RegistryError, match="not one of"):
        build(tmp_path, body)


def test_declared_but_unimplemented_api_format_fails_clearly(tmp_path):
    """ADR-0008 keeps the seam so an Anthropic adapter is additive, but the adapter does
    not exist yet -- so resolving one must say so rather than misbehave."""
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\n'
            'api_format="anthropic"\ndefault=true\n')
    with pytest.raises(registry.RegistryError, match="no adapter"):
        build(tmp_path, body)


def test_nonsense_api_format_is_refused(tmp_path):
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\n'
            'api_format="grpc"\ndefault=true\n')
    with pytest.raises(registry.RegistryError, match="not one of"):
        build(tmp_path, body)


def test_two_defaults_is_refused(tmp_path):
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\ndefault=true\n'
            '[models.b]\nbase_url="http://h:2"\nserved_model_id="t"\ndefault=true\n')
    with pytest.raises(registry.RegistryError, match="marks 2 models"):
        build(tmp_path, body)


def test_several_models_and_no_default_refuses_to_pick(tmp_path):
    """Silently choosing a model changes cost and behaviour, so it is not ours to pick."""
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\n'
            '[models.b]\nbase_url="http://h:2"\nserved_model_id="t"\n')
    with pytest.raises(registry.RegistryError, match="none marked"):
        build(tmp_path, body)


def test_a_single_model_needs_no_default_flag(tmp_path):
    reg, _ = build(tmp_path, '[models.solo]\nbase_url="http://h:1"\nserved_model_id="s"\n')
    assert reg.default_key == "solo"


def test_env_override_selects_the_default(tmp_path):
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\ndefault=true\n'
            '[models.b]\nbase_url="http://h:2"\nserved_model_id="t"\n')
    reg, _ = build(tmp_path, body, DELEGATE_DEFAULT_MODEL="b")
    assert reg.default_key == "b"


def test_env_override_naming_a_missing_model_is_refused(tmp_path):
    with pytest.raises(registry.RegistryError, match="not in"):
        build(tmp_path, GOOD, DELEGATE_DEFAULT_MODEL="nope")


def test_empty_registry_is_refused(tmp_path):
    with pytest.raises(registry.RegistryError, match="declares no models"):
        build(tmp_path, "[other]\nx=1\n")


def test_invalid_toml_is_reported_as_such(tmp_path):
    with pytest.raises(registry.RegistryError, match="not valid TOML"):
        build(tmp_path, "[models.a\nbroken")


def test_missing_registry_file_explains_where_to_get_one(tmp_path):
    cfg = config.load({
        "DELEGATE_WORKSPACE_ROOTS": str(tmp_path),
        "DELEGATE_MODELS_FILE": str(tmp_path / "absent.toml"),
    })
    with pytest.raises(registry.RegistryError, match="models.toml.example"):
        registry.load(cfg)


def test_zero_concurrency_is_refused(tmp_path):
    body = ('[models.a]\nbase_url="http://h:1"\nserved_model_id="s"\n'
            'concurrency=0\ndefault=true\n')
    with pytest.raises(registry.RegistryError, match="at least 1"):
        build(tmp_path, body)
