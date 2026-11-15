"""models.toml took, or crashed on, values of the wrong type.

Three shapes, all of them the same failure: the registry validated *names* and *ranges*
but never the TOML type of a value, so a wrong type was either accepted with a meaning
nobody wrote, or reached the validator as something it could not iterate.

(a) `fast = 5` beside the entry tables is a scalar where a table belongs. The entry
    validator's first act is `set(raw)`, so a scalar entry raised a raw TypeError out of
    `registry.load` -- a traceback at startup instead of a RegistryError naming the key.
(b) `default = "no"` was read through `bool(...)`, and a non-empty string is true, so a
    model the operator did not mark became the default.
(c) `context_window = true` passed the integer check because Python's `bool` is an `int`
    subclass, giving a one-token window; the same hole let `tokens_per_image = true`
    through, so every image was priced at one token.

Each refusal names the offending key, because the file may hold many entries and "a value
has the wrong type" is not something an operator can act on.
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


def test_a_model_entry_that_is_not_a_table_is_refused(tmp_path):
    """`fast = 5` is a scalar where `[models.fast]` belongs. It used to reach `set(raw)`."""
    # Before any [models.<key>] header, so `fast` is an entry of [models], not a field.
    body = "[models]\nfast = 5\n\n[models.a]\n" + ENTRY + "default = true\n"
    with pytest.raises(registry.RegistryError, match=r"models\.fast.*table"):
        _load(tmp_path, body)


def test_a_non_boolean_default_is_refused(tmp_path):
    """`"no"` is a non-empty string, so `bool(...)` made it the default."""
    body = "[models.a]\n" + ENTRY + 'default = "no"\n'
    with pytest.raises(registry.RegistryError, match=r"models\.a.*default.*boolean"):
        _load(tmp_path, body)


def test_a_boolean_context_window_is_refused(tmp_path):
    """`true` is an int subclass, so the range check let it through as a 1-token window."""
    body = "[models.a]\n" + ENTRY + "context_window = true\ndefault = true\n"
    with pytest.raises(
            registry.RegistryError,
            match=r"models\.a.*context_window must be an integer, not a boolean"):
        _load(tmp_path, body)


def test_a_boolean_tokens_per_image_is_refused(tmp_path):
    """Same hole as context_window: one image would have been priced at one token."""
    body = "[models.a]\n" + ENTRY + "tokens_per_image = true\ndefault = true\n"
    with pytest.raises(
            registry.RegistryError,
            match=r"models\.a.*tokens_per_image must be an integer, not a boolean"):
        _load(tmp_path, body)
