"""Config loads, validates at startup, and refuses rather than guessing.

The theme: every bad value fails at load time. A malformed timeout discovered thirty
minutes into a delegation is far worse than a refusal to boot.
"""

from __future__ import annotations

import os

import dataclasses

import pytest

from claude_delegate_local import config

ROOTS = {"DELEGATE_WORKSPACE_ROOTS": "/tmp/proj"}


def test_workspace_roots_is_required_and_has_no_default():
    """Layer 1 of the path policy. There is no safe default for "which files may a
    delegated model read", so the server refuses to start without it."""
    with pytest.raises(config.ConfigError, match="WORKSPACE_ROOTS is required"):
        config.load({})


def test_loads_with_only_the_required_setting():
    cfg = config.load(ROOTS)
    assert cfg.workspace_roots == ("/tmp/proj",)
    assert cfg.thinking_default == "low"


def test_roots_split_on_ospathsep_so_they_read_naturally_on_either_host():
    raw = os.pathsep.join(["/a", "/b", "/c"])
    cfg = config.load({"DELEGATE_WORKSPACE_ROOTS": raw})
    assert cfg.workspace_roots == ("/a", "/b", "/c")


def test_workdir_roots_falls_back_to_workspace_roots():
    cfg = config.load(ROOTS)
    assert cfg.effective_workdir_roots == cfg.workspace_roots


def test_workdir_roots_can_be_narrower_than_workspace_roots():
    cfg = config.load({**ROOTS, "DELEGATE_WORKDIR_ROOTS": "/tmp/proj/sub"})
    assert cfg.effective_workdir_roots == ("/tmp/proj/sub",)


def test_config_is_frozen_so_nothing_mutates_it_mid_delegation():
    """Asserts FrozenInstanceError, not Exception.

    `pytest.raises(Exception)` accepted any failure, including ones that say nothing about
    frozenness -- a TypeError from a changed signature, for instance. Checked while
    narrowing it: assigning a *misspelled* field also raises FrozenInstanceError rather
    than AttributeError, so that particular escape does not exist here. The point stands
    anyway; the assertion should name the thing being proved.
    """
    cfg = config.load(ROOTS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_tokens = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "key,value,expect",
    [
        ("DELEGATE_THINKING_DEFAULT", "maximum", "not one of"),
        ("DELEGATE_TRANSPORT", "websocket", "not one of"),
        ("DELEGATE_TURN_TIMEOUT", "soon", "not an integer"),
        ("DELEGATE_RESEND_REASONING", "maybe", "not a boolean"),
        ("DELEGATE_RETRY_BASE_DELAY", "slow", "not a number"),
    ],
)
def test_malformed_values_are_refused_at_load_time(key, value, expect):
    with pytest.raises(config.ConfigError, match=expect):
        config.load({**ROOTS, key: value})


def test_bad_effort_is_refused_at_load_naming_the_accepted_set():
    """Refused early and cheaply, with the allowed values in the message.

    This test asserts nothing about what the server would do with a value it does not
    recognise. Its previous name and body did, and the claim they encoded was false --
    which is why correcting that error message broke this test rather than a real one.
    A test should pin behaviour, never the prose explaining it; the explanation lives in
    docs/ARCHITECTURE.md, which owns the backend layer.
    """
    with pytest.raises(config.ConfigError, match=r"not one of.*Refused at load"):
        config.load({**ROOTS, "DELEGATE_THINKING_DEFAULT": "maxx"})


def test_inherit_is_not_a_configurable_default():
    """It is a caller's word, not a level, and this is the end of the chain it defers to.

    Accepting it here would make the fallback point at itself -- `thinking_default` is what
    `"inherit"` resolves *to*, so a config set to it has nothing left to resolve (ADR-0045).
    """
    with pytest.raises(config.ConfigError, match="not one of"):
        config.load({**ROOTS, "DELEGATE_THINKING_DEFAULT": config.EFFORT_INHERIT})


def test_turn_budget_cannot_exceed_the_hard_cap():
    with pytest.raises(config.ConfigError, match="exceeds"):
        config.load({**ROOTS, "DELEGATE_MAX_TURNS_DEFAULT": "500"})


def test_a_turn_cannot_outlive_the_delegation_containing_it():
    with pytest.raises(config.ConfigError, match="outlive"):
        config.load({**ROOTS,
                     "DELEGATE_TURN_TIMEOUT": "7200",
                     "DELEGATE_DISPATCH_TIMEOUT": "3600"})


def test_a_keepalive_that_cannot_hold_the_idle_timer_off_is_refused():
    """The interval is a correctness setting, and the failure it causes is invisible from
    inside the server.

    Measured 2026-09-01: at the client's idle timeout the caller abandons the call and
    nothing reaches the server -- no cancellation, no EOF -- so the dispatch runs on
    holding its admission slot until the work ends on its own. A one-shot sends nothing
    but this heartbeat, so an interval that cannot beat inside the window silently buys
    the lockout. Refusing at startup is the only place it can be caught.
    """
    with pytest.raises(config.ConfigError, match="leaves no margin"):
        config.load({**ROOTS, "DELEGATE_KEEPALIVE_INTERVAL": "1700"})


def test_a_keepalive_with_room_to_beat_twice_is_accepted():
    """The other direction, and the boundary itself. Half the idle timeout is the rule, so
    exactly half must pass -- a check that also refused the largest legal value would be
    one nobody could satisfy by reading the error."""
    half = config.CLIENT_STDIO_IDLE_TIMEOUT // 2
    cfg = config.load({**ROOTS, "DELEGATE_KEEPALIVE_INTERVAL": str(half)})
    assert cfg.keepalive_interval == half
    assert config.load(dict(ROOTS)).keepalive_interval < half, "the default must pass too"


def test_the_connect_phase_cannot_outlast_the_call_it_belongs_to():
    with pytest.raises(config.ConfigError, match="outlast"):
        config.load({**ROOTS,
                     "DELEGATE_CONNECT_TIMEOUT": "600",
                     "DELEGATE_TURN_TIMEOUT": "300"})


def test_connect_timeout_must_be_positive():
    with pytest.raises(config.ConfigError, match="DELEGATE_CONNECT_TIMEOUT must be positive"):
        config.load({**ROOTS, "DELEGATE_CONNECT_TIMEOUT": "0"})


def test_connect_timeout_reads_its_environment_variable():
    cfg = config.load({**ROOTS, "DELEGATE_CONNECT_TIMEOUT": "5"})
    assert cfg.connect_timeout == 5


def test_total_prefetch_below_per_file_would_admit_nothing():
    with pytest.raises(config.ConfigError, match="could ever be prefetched"):
        config.load({**ROOTS,
                     "DELEGATE_MAX_FILE_TOKENS": "50000",
                     "DELEGATE_MAX_TOTAL_PREFETCH_TOKENS": "1000"})


def test_extensions_are_normalised_to_lowercase_with_a_leading_dot():
    cfg = config.load({**ROOTS,
                       "DELEGATE_EXT_ALLOWLIST": os.pathsep.join(["py", ".MD", "TS"])})
    assert cfg.ext_allowlist == (".py", ".md", ".ts")


# --------------------------------------------------------------- token estimation


def test_estimator_is_extension_aware_because_bytes_are_not_the_unit_that_matters():
    """ADR-0019. The same byte count is worth far more tokens as JSON than as Python,
    so a byte-denominated budget rations the wrong thing."""
    cfg = config.load(ROOTS)
    nbytes = 131072
    py = cfg.estimate_tokens(nbytes, ".py")
    js = cfg.estimate_tokens(nbytes, ".json")
    assert js > py * 1.8, f"json should cost far more tokens per byte: {py=} {js=}"


def test_unknown_extension_uses_the_worst_observed_ratio():
    """Guessing high wastes a little admission capacity; guessing low queues a request
    until it times out. So an unknown type is costed pessimistically."""
    cfg = config.load(ROOTS)
    unknown = cfg.estimate_tokens(100_000, ".unheard-of")
    worst = cfg.estimate_tokens(100_000, ".json")
    assert unknown >= worst * 0.95


def test_estimates_never_undercount_the_measured_ratios():
    """Each table entry is rounded DOWN from measurement, so the token estimate is at
    or above the true count. Measured: json 1.78, toml 2.08, md 3.42, py 3.89."""
    cfg = config.load(ROOTS)
    measured = {".json": 1.78, ".toml": 2.08, ".md": 3.42, ".py": 3.89}
    for ext, real_ratio in measured.items():
        nbytes = 200_000
        true_tokens = nbytes / real_ratio
        assert cfg.estimate_tokens(nbytes, ext) >= true_tokens * 0.98, (
            f"{ext}: estimate undercounts the measured ratio"
        )


# --------------------------------------------------------------- doc generation input


def test_describe_covers_every_field_so_no_setting_can_be_undocumented():
    from dataclasses import fields

    described = {r["field"] for r in config.describe()}
    assert described == {f.name for f in fields(config.Config)}


def test_every_setting_carries_a_description():
    missing = [r["env"] for r in config.describe() if not r["description"].strip()]
    assert not missing, f"settings with no description: {missing}"


def test_env_names_are_unique():
    names = [r["env"] for r in config.describe()]
    assert len(names) == len(set(names))


def test_admission_wait_timeout_must_be_positive():
    with pytest.raises(
        config.ConfigError, match="DELEGATE_ADMISSION_WAIT_TIMEOUT must be positive"
    ):
        config.load({**ROOTS, "DELEGATE_ADMISSION_WAIT_TIMEOUT": "0"})


@pytest.mark.parametrize(
    "name",
    [
        "DELEGATE_MAX_INFLIGHT_SEQS",
        "DELEGATE_KV_TOKEN_BUDGET",
        "DELEGATE_LARGE_PREFILL_TOKENS",
        "DELEGATE_MAX_INFLIGHT_LARGE_PREFILLS",
    ],
)
def test_an_admission_limit_of_zero_is_refused_at_load(name):
    """Zero does not mean unlimited here; it means nothing is ever admitted.

    A gate whose predicate can never be satisfied does not fail -- it queues every
    delegation until the wait times out, and reports congestion for a setting that is
    simply wrong. Refused at load, where the operator is still looking at it.
    """
    with pytest.raises(config.ConfigError, match=f"{name} must be positive"):
        config.load({**ROOTS, name: "0"})
