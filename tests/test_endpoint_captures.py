"""The endpoint capture scripts, and the two layers that keep a capture unpublished.

Neither script is exercised against a real endpoint here, deliberately: `probe_endpoint`
needs the cluster, and a test that needs the cluster up fails when it is down while saying
nothing about the repository (ADR-0052). What is tested is everything that runs offline —
the diff, the shape and metric helpers, and the enforcement.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def diff():
    return _script("diff_endpoint_captures")


@pytest.fixture(scope="module")
def probe():
    return _script("probe_endpoint")


# --- the diff reports structure -----------------------------------------------------


def test_a_field_added_removed_or_retyped_is_reported(diff):
    old = {"a": "int = 1", "b": "NoneType = None", "gone": "str = 'x'"}
    new = {"a": "int = 2", "b": "str = 'now a string'", "fresh": "int = 0"}

    result = diff.compare_fields(old, new)

    assert result["added"] == ["fresh"]
    assert result["removed"] == ["gone"]
    assert result["type-changed"] == ["b: NoneType -> str"]


def test_a_value_that_changed_without_its_type_is_not_a_difference(diff):
    """The whole point of comparing structure. `cached_tokens` moves on every call and a
    tool that reported it as a change would report a difference on every run, which is the
    same as reporting none."""
    old = {"usage.cached_tokens": "int = 0"}
    new = {"usage.cached_tokens": "int = 44800"}

    assert not any(diff.compare_fields(old, new).values())


def test_the_diff_cannot_print_a_value(diff, tmp_path, capsys, monkeypatch):
    """The security property, asserted rather than assumed.

    A capture is never scanned by anything -- it is never tracked -- so this tool's output
    is the one place a captured value could escape into a paste. Every recorded value here
    is a distinctive string, and none of them may appear in what the tool writes.
    """
    secrets = ["swordfish-prompt-echo", "hunter2-transfer-target", "0xC0FFEE-fingerprint"]
    old = {
        "captured": "2026-09-01", "served_model_id": "model-a",
        "normal": {"fields": {
            "prompt_text": f"str (value withheld) {secrets[0]}",
            "kv_transfer_params": f"str = '{secrets[1]}'",
            "system_fingerprint": f"str = '{secrets[2]}'",
        }},
        "metrics": {"names": ["vllm:gone_metric"]},
    }
    new = {
        "captured": "2026-09-05", "served_model_id": "model-b",
        "normal": {"fields": {
            "prompt_text": "NoneType (value withheld)",
            "kv_transfer_params": "NoneType = None",
            "system_fingerprint": "str = 'a different fingerprint entirely'",
        }},
        "metrics": {"names": ["vllm:new_metric"]},
    }
    a, b = tmp_path / "old.json", tmp_path / "new.json"
    a.write_text(json.dumps(old), encoding="utf-8")
    b.write_text(json.dumps(new), encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["diff", str(a), str(b)])
    diff.main()
    printed = capsys.readouterr().out

    for secret in secrets:
        assert secret not in printed, f"a captured value reached the output: {secret}"
    assert "a different fingerprint entirely" not in printed
    # It still did its job: a type change and both metric movements are reported.
    assert "NoneType" in printed
    assert "vllm:new_metric" in printed and "vllm:gone_metric" in printed


def test_a_withheld_value_and_a_permitted_one_compare_the_same_way(diff):
    """`shape()` writes two forms, `int = 5` and `NoneType (value withheld)`. Both must
    yield a bare type, or a capture would appear to change type when only its permission
    to record a value changed."""
    assert diff._type_of("int = 44800") == "int"
    assert diff._type_of("NoneType (value withheld)") == "NoneType"
    assert diff._type_of("str = 'has = an equals sign'") == "str"


# --- what the probe is allowed to record --------------------------------------------


def test_a_prompt_echoing_field_is_recorded_by_type_only(probe):
    """These echo the prompt, which for this server is repository source. A capture is
    never scanned, so this allowlist is the only thing standing between a repository and a
    file somebody later pastes somewhere."""
    payload = {
        "prompt_text": "the entire files block",
        "prompt_token_ids": [1, 2, 3],
        "kv_transfer_params": {"host": "somewhere"},
        "usage": {"prompt_tokens": 44905},
    }
    fields = probe.shape(payload)

    assert fields["prompt_text"] == "str (value withheld)"
    assert fields["prompt_token_ids"] == "list (value withheld)"
    assert fields["kv_transfer_params"] == "dict (value withheld)"
    assert "the entire files block" not in json.dumps(fields)
    assert "somewhere" not in json.dumps(fields)
    # A permitted field still carries its value, or the capture answers nothing.
    assert fields["usage.prompt_tokens"] == "int = 44905"


def test_metric_labels_are_stripped_and_names_are_kept(probe):
    """Labels carry configuration and handler paths. Stripping them is a rule, not a side
    effect of wanting names -- so a labelled metric must reduce to its name and nothing."""
    text = "\n".join([
        "# HELP vllm:cache_config_info Cache config",
        'vllm:cache_config_info{block_size="4",gpu_memory_utilization="0.835"} 1.0',
        'http_requests_total{handler="/v1/chat/completions",method="POST"} 12.0',
        "vllm:num_requests_running 1.0",
    ])
    names = probe.metric_names(text)

    assert names == ["http_requests_total", "vllm:cache_config_info",
                     "vllm:num_requests_running"]
    assert not any("0.835" in n or "chat/completions" in n for n in names)


# --- the enforcement ----------------------------------------------------------------


def test_the_denylist_covers_a_capture_and_therefore_hides_it_from_the_model():
    """One list, two enforcers: the gate refuses to let a capture be tracked and layer 3
    refuses to let the local model read one. The second is the accepted cost of the first
    (ADR-0052), so this asserts the glob rather than either consumer -- remove it and both
    protections vanish together, which is exactly why they share a file."""
    globs = [
        line.strip()
        for line in (ROOT / "security" / "secret_globs.txt").read_text(
            encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    capture = "local/endpoint-captures/endpoint-2026-09-05-a-model.json"

    assert any(fnmatch.fnmatch(capture, g) for g in globs), (
        "a capture is no longer on the denylist; the gate would let it be committed "
        "and the local model could read it"
    )
