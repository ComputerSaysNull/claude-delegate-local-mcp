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
        ("DELEGATE_TRANSPORT", "websocket", "is refused; this server implements"),
        ("DELEGATE_MAX_TURNS_DEFAULT", "soon", "not an integer"),
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


def test_the_connect_phase_cannot_outlast_the_silence_budget_containing_it():
    """A route that never connects is silence, so the connect bound belongs inside it.

    It used to be checked against `turn_timeout`, which is retired. Left unchecked, a
    connect bound above the stall budget would mean a blackholed route was reported as a
    stall rather than as a connect failure -- the diagnosis sent to an operator would
    name the wrong layer.
    """
    with pytest.raises(config.ConfigError, match="outlast"):
        config.load({**ROOTS,
                     "DELEGATE_CONNECT_TIMEOUT": "600",
                     "DELEGATE_STALL_TIMEOUT": "300"})


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


def test_admission_wait_timeout_may_be_zero():
    """0 is how an operator asks for a wait with no bound, and is the default (ADR-0093).

    It was required positive until 2026-09-19, which meant no configuration could express
    "as long as the work ahead takes" and every setup carried a bail-out.
    """
    cfg = config.load({**ROOTS, "DELEGATE_ADMISSION_WAIT_TIMEOUT": "0"})
    assert cfg.admission_wait_timeout == 0


def test_admission_wait_timeout_must_not_be_negative():
    """0 is off; a negative is a mistake rather than a stronger off."""
    with pytest.raises(
        config.ConfigError, match="DELEGATE_ADMISSION_WAIT_TIMEOUT must not be negative"
    ):
        config.load({**ROOTS, "DELEGATE_ADMISSION_WAIT_TIMEOUT": "-1"})


@pytest.mark.parametrize(
    "name",
    [
        "DELEGATE_MAX_INFLIGHT_SEQS",
        "DELEGATE_KV_TOKEN_BUDGET",
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


def test_the_http_transport_is_refused_rather_than_started():
    """A knob advertised as unfinished must not start a server.

    `streamable-http` was an accepted value, so setting it ran a listener that nothing
    here authenticates -- the shape ADR-0034 deleted `sandbox_enabled` for. Measured, the
    reachable surface was other local processes rather than the network, because FastMCP
    defaults its host to loopback and only a port was ever passed; no token is the true
    half of the finding, and it is enough.

    Refused rather than the field being deleted, which is the divergence from ADR-0034 and
    the reason `test_a_stale_transport_value_is_not_silently_ignored` exists below.
    """
    with pytest.raises(config.ConfigError, match="is refused; this server implements"):
        config.load({**ROOTS, "DELEGATE_TRANSPORT": "streamable-http"})


def test_a_stale_transport_value_is_not_silently_ignored():
    """Why the field is kept rather than deleted.

    `load` reads only variables that match a dataclass field, so an unknown `DELEGATE_*`
    name is discarded without a word -- verified here with a deliberate typo, which is
    also the sharper end of the same behaviour. Deleting `transport` would therefore turn
    a configuration error into silence: the operator sets the variable, gets stdio, and is
    told nothing. Keeping it is what makes the refusal above reachable.
    """
    # The typo is ignored, which is the behaviour that makes deletion the wrong remedy.
    assert config.load({**ROOTS, "DELEGATE_TRANSPROT": "streamable-http"}).transport == "stdio"
    # The real name is not.
    with pytest.raises(config.ConfigError):
        config.load({**ROOTS, "DELEGATE_TRANSPORT": "streamable-http"})


def test_the_port_only_the_http_transport_used_is_gone():
    """Deleted rather than kept inert. A port that nothing can listen on renders in the
    generated reference as a knob that does something (ADR-0034)."""
    assert not hasattr(config.load(ROOTS), "http_port")
    assert "http_port" not in {f.name for f in dataclasses.fields(config.Config)}


# --- the deadlines no longer nest through stall (ADR-0099) -----------------------------


def test_a_small_stall_budget_under_a_large_ceiling_is_permitted():
    """The link stall used to have to a per-call bound, and no longer should.

    It was written when stall meant "has not COMPLETED a turn" (ADR-0047): under that
    reading a stall shorter than one call would cut short a legitimately slow one. Since
    ADR-0072 the signal is token arrival, so a slow call that is producing never stalls,
    and the reason the bound existed went with it -- ahead of the setting itself.
    """
    cfg = config.load({**ROOTS, "DELEGATE_STALL_TIMEOUT": "600"})
    assert cfg.stall_timeout == 600
    assert cfg.dispatch_timeout > cfg.stall_timeout, "the ceiling must still sit above it"


def test_a_stall_budget_above_the_dispatch_timeout_is_still_refused():
    """The negative control. Only the lower bound is gone; a stall that can never fire is
    still a misconfiguration, and dropping both checks would look identical here."""
    with pytest.raises(config.ConfigError, match="STALL_TIMEOUT"):
        config.load({**ROOTS, "DELEGATE_STALL_TIMEOUT": "99999",
                     "DELEGATE_DISPATCH_TIMEOUT": "3600"})


# --- sampling: one pair, at the values this model was evaluated at (ADR-0098) ----------


def test_the_evaluated_sampling_pair_is_the_default():
    """1.0 / 0.95 is DeepSeek's own evaluation of this model, not a working point.

    One pair rather than two: the split existed only to hold the loop low against
    malformed tool calls, and that premise was measured false across 96 calls from 0.2
    to 1.5 -- not one was malformed.
    """
    cfg = config.load(ROOTS)
    assert cfg.temperature == 1.0
    assert cfg.top_p == 0.95


def test_a_retired_temperature_name_is_refused_rather_than_ignored():
    """The sharp end of `load` reading only names that match a field.

    Both retired names are live in real `.env` files today. Deleting the fields would
    make those lines do nothing without a word, and the operator would run 1.0 believing
    they set 0.7 -- so they are kept and refused, exactly as `transport` is.
    """
    for name in ("DELEGATE_TOOL_CALL_TEMPERATURE", "DELEGATE_ONE_SHOT_TEMPERATURE"):
        with pytest.raises(config.ConfigError, match="retired"):
            config.load({**ROOTS, name: "0.7"})


def test_the_retired_per_call_deadline_is_refused_rather_than_ignored():
    """`DELEGATE_TURN_TIMEOUT` is live in every `.env` written before it went.

    Ignoring the line would leave an operator believing they had bounded a call, which
    is the exact belief the retirement removes: nothing bounds a call by length any
    more. The refusal has to name what does the job instead, or the operator has no
    move.
    """
    with pytest.raises(config.ConfigError, match="retired") as caught:
        config.load({**ROOTS, "DELEGATE_TURN_TIMEOUT": "1800"})
    assert "DELEGATE_STALL_TIMEOUT" in str(caught.value)


def test_every_retired_field_has_a_remedy():
    """The roster and the remedies are two lists that must agree.

    `_check_retired` indexes one by the other, so a field added to the roster alone
    raises `KeyError` at load -- from inside the check that exists to produce a readable
    refusal. Cheaper to catch here than in an operator's terminal.
    """
    assert set(config.RETIRED_FIELDS) == set(config.RETIRED_REMEDY)
    for name in config.RETIRED_FIELDS:
        assert config.RETIRED_REMEDY[name].strip(), name


def test_the_retirement_refusal_is_specific_and_not_a_blanket():
    """The negative control for the test above.

    A refusal that fired on any unrecognised `DELEGATE_*` would pass that test while
    breaking every deployment with a stale line in its `.env`. A typo must still be
    ignored in silence, which is the behaviour ADR-0034 reasoned about.
    """
    assert config.load({**ROOTS, "DELEGATE_TOOL_CALL_TEMPERATURF": "0.7"}).temperature == 1.0


def test_top_p_is_bounded_to_the_unit_interval():
    """Mirrors the endpoint, which answers 400 to `top_p=5.0` -- measured 2026-09-21.

    Refused here rather than at the wire, so the operator learns at startup rather than
    thirty minutes into a delegation.
    """
    for bad in ("1.01", "-0.1"):
        with pytest.raises(config.ConfigError, match="TOP_P"):
            config.load({**ROOTS, "DELEGATE_TOP_P": bad})
    assert config.load({**ROOTS, "DELEGATE_TOP_P": "0.5"}).top_p == 0.5
