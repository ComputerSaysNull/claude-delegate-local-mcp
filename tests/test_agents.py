"""Agent files: the lookup, the frontmatter, and every refusal proving it can fire.

The rule this file is written to (CLAUDE.md): a check that cannot fail is worse than no
check, because it is trusted. So every validation here has a test that feeds it a real
violation and asserts it is refused -- not a test that feeds it something valid and asserts
nothing went wrong. Four checks in this repository have already been found unable to fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import agents
from claude_delegate_local.agents import AgentError
from claude_delegate_local.config import Config
from claude_delegate_local.paths import path_within_roots
from claude_delegate_local.sandbox import base_mount_targets
from claude_delegate_local.loop import SYSTEM_PROMPT_ONE_SHOT, Delegation, build_one_shot_request


def cfg(tmp_path: Path, **over) -> Config:
    kw = {
        "workspace_roots": (str(tmp_path),),
        "agents_dir": str(tmp_path / "home" / "agents"),
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL = "---\nname: helper\n---\n\nYou help.\n"

UNPROVEN_SYMLINK = (
    "SYMLINK RESOLUTION UNPROVEN BY THIS RUN -- this is not a pass. Creating a symlink "
    "needs a privilege Windows withholds by default, and the server runs in WSL. Run it "
    "there -- see CONTRIBUTING.md."
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN_SYMLINK)


# --- the three-tier lookup ----------------------------------------------------------------


def test_each_tier_resolves_when_it_is_the_only_one(tmp_path):
    c = cfg(tmp_path)
    work = tmp_path / "proj"

    write(work / ".claude" / "agents" / "helper.md", MINIMAL)
    assert agents.find_agent_file(c, "helper", str(work)).name == "helper.md"

    (work / ".claude" / "agents" / "helper.md").unlink()
    write(work / ".claude" / "skills" / "helper" / "SKILL.md", MINIMAL)
    assert agents.find_agent_file(c, "helper", str(work)).name == "SKILL.md"

    (work / ".claude" / "skills" / "helper" / "SKILL.md").unlink()
    write(Path(c.agents_dir) / "helper.md", MINIMAL)
    assert agents.find_agent_file(c, "helper", str(work)).parent == Path(c.agents_dir)


def test_a_project_agent_shadows_the_personal_one_of_the_same_name(tmp_path):
    """The point of the three tiers. A repository ships an agent that knows its own
    conventions without anyone editing their home directory."""
    c = cfg(tmp_path)
    work = tmp_path / "proj"
    write(work / ".claude" / "agents" / "helper.md", "---\nname: helper\n---\nPROJECT\n")
    write(work / ".claude" / "skills" / "helper" / "SKILL.md", "---\nname: helper\n---\nSKILL\n")
    write(Path(c.agents_dir) / "helper.md", "---\nname: helper\n---\nPERSONAL\n")

    assert agents.load_agent(c, "helper", str(work)).body == "PROJECT"

    (work / ".claude" / "agents" / "helper.md").unlink()
    assert agents.load_agent(c, "helper", str(work)).body == "SKILL"


def test_a_bad_name_is_refused_before_the_filesystem_is_touched(tmp_path, monkeypatch):
    """The traversal case, asserted as reachability rather than as a raise.

    `Path.is_file` is made to explode, so if the lookup ever puts an unvalidated name in
    front of the filesystem this test fails with that error rather than with the refusal.
    A `pytest.raises` alone would not distinguish the two.

    What this pins down, exactly: no attacker-controlled name reaches the filesystem.
    It does not pin down where in the function the check sits -- building the candidate
    paths is pure, so a check placed after that and before the I/O is equally safe. The
    guarantee worth having is about the I/O, and that is the one asserted. Verified by
    deleting the check and watching this fail on the `../../etc/passwd.md` candidate.
    """
    def boom(self):
        raise AssertionError(f"the filesystem was reached with {self!r}")

    monkeypatch.setattr(Path, "is_file", boom)
    c = cfg(tmp_path)
    for bad in ("../../etc/passwd", "foo/bar", "foo\\bar", "..", "", "a b", "x/../y"):
        with pytest.raises(AgentError, match="not a valid agent name"):
            agents.find_agent_file(c, bad, str(tmp_path))


def test_not_found_names_every_place_it_looked(tmp_path):
    c = cfg(tmp_path)
    with pytest.raises(AgentError) as e:
        agents.find_agent_file(c, "missing", str(tmp_path / "proj"))
    message = str(e.value)
    assert "missing.md" in message
    assert "agents" in message
    assert "skills" in message and "SKILL.md" in message
    assert str(c.agents_dir) in message


# --- frontmatter: the shape ---------------------------------------------------------------


def test_every_field_parses_to_the_right_type(tmp_path):
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "full.md", (
        "---\n"
        "name: full\n"
        "description: Does the thing.\n"
        "model: deepseek-v4-flash\n"
        "effort: low\n"
        "max_turns: 20\n"
        "max_tokens: 32768\n"
        "keep_tool_results: 6\n"
        "allowed_tools: [read_file, write_file, run_bash]\n"
        "network: false\n"
        "extra_binds: []\n"
        "---\n"
        "\nYou write tests.\n"
    ))
    spec = agents.load_agent(c, "full")
    assert spec.description == "Does the thing."
    assert spec.model == "deepseek-v4-flash"
    assert spec.effort == "low"
    assert (spec.max_turns, spec.max_tokens, spec.keep_tool_results) == (20, 32768, 6)
    assert spec.allowed_tools == ("read_file", "write_file", "run_bash")
    assert spec.network is False
    assert spec.extra_binds == ()
    assert spec.body == "You write tests."


def test_an_unspecified_list_is_not_an_empty_one(tmp_path):
    """`None` means the file did not say; `()` means it said no tools. Conflating them turns
    an agent that asked for nothing into one that gets everything, or the reverse."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "silent.md", MINIMAL.replace("helper", "silent"))
    write(Path(c.agents_dir) / "none.md", "---\nname: none\nallowed_tools: []\n---\nbody\n")
    assert agents.load_agent(c, "silent").allowed_tools is None
    assert agents.load_agent(c, "none").allowed_tools == ()


def test_minimal_frontmatter_leaves_everything_else_unset(tmp_path):
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "helper.md", MINIMAL)
    spec = agents.load_agent(c, "helper")
    assert (spec.model, spec.effort, spec.max_turns, spec.max_tokens) == (None, None, None, None)
    assert spec.keep_tool_results is None and spec.allowed_tools is None
    assert spec.network is False and spec.extra_binds == ()


def test_the_body_is_exactly_what_follows_the_closing_marker(tmp_path):
    """No field bleeds into the prompt, and a '---' inside the body is not a second block."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "helper.md",
          "---\nname: helper\nmodel: m\n---\nFirst.\n\n---\n\nStill body.\n")
    assert agents.load_agent(c, "helper").body == "First.\n\n---\n\nStill body."


# --- frontmatter: every refusal, each proven against a real violation ----------------------


def test_an_unknown_key_is_refused_rather_than_dropped(tmp_path):
    """The ancestor bug's actual shape: a plausible typo, not a strawman. `mode:` for
    `model:` reads correctly and would silently cost the setting."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "typo.md", "---\nname: typo\nmode: deepseek-v4-flash\n---\nbody\n")
    with pytest.raises(AgentError, match="unknown frontmatter key"):
        agents.load_agent(c, "typo")


def test_claude_codes_own_effort_spelling_is_refused(tmp_path):
    """`medium` is valid in Claude Code's agent format and deliberately not in this one
    (ADR-0031). Silently accepting it is how a borrowed file appears to work while the
    effort it names has no translation."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "m.md", "---\nname: m\neffort: medium\n---\nbody\n")
    with pytest.raises(AgentError) as e:
        agents.load_agent(c, "m")
    assert "medium" in str(e.value) and "ADR-0031" in str(e.value)

    write(Path(c.agents_dir) / "m2.md", "---\nname: m2\neffort: med\n---\nbody\n")
    with pytest.raises(AgentError, match="is not one of"):
        agents.load_agent(c, "m2")


def test_inherit_is_not_an_agent_effort(tmp_path):
    """An agent file is one of the tiers `"inherit"` defers to, so it cannot itself defer
    with that word -- it either binds a level or says nothing at all (ADR-0045)."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "i.md", "---\nname: i\neffort: inherit\n---\nbody\n")
    with pytest.raises(AgentError, match="is not one of"):
        agents.load_agent(c, "i")


def test_a_tool_this_server_does_not_implement_is_refused(tmp_path):
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "t.md",
          "---\nname: t\nallowed_tools: [read_file, delete_everything]\n---\nbody\n")
    with pytest.raises(AgentError) as e:
        agents.load_agent(c, "t")
    assert "delete_everything" in str(e.value)


def test_max_turns_over_the_hard_cap_is_refused_at_load_not_clamped(tmp_path):
    """Deliberately unlike `resolve_max_turns`, which clamps a caller's number in silence.

    A call argument is transient. A file is committed, read again, and trusted, so a silent
    clamp would leave a wrong number sitting in it indefinitely and appearing to work. The
    asymmetry is the point, so it is asserted rather than assumed.
    """
    c = cfg(tmp_path)
    cap = c.max_turns_hard_cap
    write(Path(c.agents_dir) / "greedy.md", f"---\nname: greedy\nmax_turns: {cap + 1}\n---\nb\n")
    with pytest.raises(AgentError, match="exceeds DELEGATE_MAX_TURNS_HARD_CAP"):
        agents.load_agent(c, "greedy")

    write(Path(c.agents_dir) / "atcap.md", f"---\nname: atcap\nmax_turns: {cap}\n---\nb\n")
    assert agents.load_agent(c, "atcap").max_turns == cap


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("max_turns: 0", "at least 1"),
        ("max_tokens: 0", "at least 1"),
        ("keep_tool_results: -1", "cannot be negative"),
        ("max_turns: soon", "not a whole number"),
        ("network: perhaps", "not a boolean"),
        ("allowed_tools: read_file", "is not a list"),
        ("extra_binds: [relative/path]", "not an absolute path"),
    ],
)
def test_each_range_and_type_rule_refuses_a_real_violation(tmp_path, frontmatter, expected):
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "bad.md", f"---\nname: bad\n{frontmatter}\n---\nbody\n")
    with pytest.raises(AgentError, match=expected):
        agents.load_agent(c, "bad")


def test_a_declared_name_that_disagrees_with_the_filename_is_refused(tmp_path):
    """Callers look an agent up by filename, so a 'name:' that says otherwise is one of the
    two lying, and which one is not this format's to guess."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "actual.md", "---\nname: something-else\n---\nbody\n")
    with pytest.raises(AgentError, match="declares name="):
        agents.load_agent(c, "actual")


def test_a_file_with_no_frontmatter_is_refused(tmp_path):
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "plain.md", "Just a prompt, no block.\n")
    with pytest.raises(AgentError, match="no frontmatter"):
        agents.load_agent(c, "plain")


def test_the_flat_format_refuses_what_it_cannot_read(tmp_path):
    """A nested block is YAML this parser does not implement. Refusing beats reading it as
    something else, which is the failure mode a hand-written parser has to design out."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "nested.md",
          "---\nname: nested\nlimits:\n  max_turns: 5\n---\nbody\n")
    with pytest.raises(AgentError, match="indented"):
        agents.load_agent(c, "nested")


def test_a_key_set_twice_is_refused(tmp_path):
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "dup.md", "---\nname: dup\nmodel: a\nmodel: b\n---\nbody\n")
    with pytest.raises(AgentError, match="twice"):
        agents.load_agent(c, "dup")


# --- discovery ----------------------------------------------------------------------------


def test_list_agents_reports_the_tier_that_would_actually_be_used(tmp_path):
    c = cfg(tmp_path)
    work = tmp_path / "proj"
    write(work / ".claude" / "agents" / "shared.md", "---\nname: shared\n---\nPROJECT\n")
    write(Path(c.agents_dir) / "shared.md", "---\nname: shared\n---\nPERSONAL\n")
    write(Path(c.agents_dir) / "personal-only.md", "---\nname: personal-only\n---\nP\n")

    found = {a.name: a for a in agents.list_agents(c, str(work))}
    assert set(found) == {"shared", "personal-only"}
    assert found["shared"].body == "PROJECT", "a shadowed agent must not be listed twice"


def test_one_broken_definition_does_not_hide_the_others(tmp_path):
    """Discovery is not validation. A single unparseable file in a personal directory should
    not make every other agent undiscoverable -- the broken one is simply absent."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "good.md", "---\nname: good\n---\nfine\n")
    write(Path(c.agents_dir) / "broken.md", "---\nname: broken\nnonsense: 1\n---\nb\n")
    assert {a.name for a in agents.list_agents(c)} == {"good"}
    # ...and asking for it by name still says exactly what is wrong with it.
    with pytest.raises(AgentError, match="unknown frontmatter key"):
        agents.load_agent(c, "broken")


def test_a_broken_definition_is_named_rather_than_merely_absent(tmp_path):
    """Skipping it is right; skipping it silently was not.

    "No such agent" and "that agent is broken" were the same answer, and the standing
    advice for the first -- ask by name and read the error -- needs the name that the
    omission hides.
    """
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "good.md", "---\nname: good\n---\nfine\n")
    write(Path(c.agents_dir) / "broken.md",
          "---\nname: broken\nnonsense: 1\n---\nb\n")

    listing = agents.survey_agents(c)
    assert {a.name for a in listing.agents} == {"good"}
    assert [s.name for s in listing.skipped] == ["broken"]
    assert "unknown frontmatter key" in listing.skipped[0].reason
    assert listing.skipped[0].source_path.endswith("broken.md")
    assert listing.other_format == ()


def test_nothing_is_reported_as_skipped_when_every_file_parses(tmp_path):
    """The other direction. A list that always reported something would pass the test
    above while making every healthy directory look broken."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "good.md", "---\nname: good\n---\nfine\n")
    listing = agents.survey_agents(c)
    assert {a.name for a in listing.agents} == {"good"}
    assert listing.skipped == ()
    assert listing.other_format == ()


def test_a_shadowed_name_is_not_reported_as_skipped(tmp_path):
    """Being overridden is not being broken. The lookup really does offer only one, so
    reporting the loser would describe a choice that does not exist."""
    c = cfg(tmp_path)
    work = tmp_path / "proj"
    write(work / ".claude" / "agents" / "dup.md", "---\nname: dup\n---\nnear\n")
    write(Path(c.agents_dir) / "dup.md", "---\nname: dup\n---\nfar\n")

    listing = agents.survey_agents(c, str(work))
    assert [a.name for a in listing.agents] == ["dup"]
    assert "near" in listing.agents[0].body
    assert listing.skipped == ()
    assert listing.other_format == ()


def test_a_claude_code_agent_is_reported_as_the_other_format_not_as_broken(tmp_path):
    """Measured on this repository, 2026-09-02: four of its five agent files carry `tools`.

    They are Claude Code's, deliberately (ADR-0031), so calling them broken would leave
    the broken list permanently non-empty here -- and a list that is never empty is one
    nobody reads, which would cost exactly the visibility this reporting was added for.
    """
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "theirs.md",
          "---\nname: theirs\ntools: Read, Grep\n---\nbody\n")
    write(Path(c.agents_dir) / "mine.md", "---\nname: mine\n---\nbody\n")

    listing = agents.survey_agents(c)
    assert [a.name for a in listing.agents] == ["mine"]
    assert listing.skipped == (), "a foreign format is not a fault"
    assert [f.name for f in listing.other_format] == ["theirs"]
    assert listing.other_format[0].foreign_keys == ("tools",)


def test_a_file_in_this_format_with_a_typo_is_still_broken(tmp_path):
    """The other direction for that split. Suppressing every unknown key as "someone
    else's format" would re-hide the typo this whole report exists to surface."""
    c = cfg(tmp_path)
    write(Path(c.agents_dir) / "typo.md",
          "---\nname: typo\nallowd_tools: [read_file]\n---\nbody\n")

    listing = agents.survey_agents(c)
    assert listing.agents == ()
    assert [s.name for s in listing.skipped] == ["typo"]
    assert listing.other_format == ()


def test_the_agent_body_never_enters_the_system_prompt():
    """ADR-0011 asserted directly rather than by inspection.

    The system prompt is a byte-for-byte constant the cluster caches; one dynamic byte
    disables that with no error and no symptom beyond slower prefill. An agent body is
    per-delegation text that reads exactly like a system prompt, which makes it the most
    likely thing to be put in one.
    """
    d = Delegation(task="the task", files_block="THE FILES", agent_body="THE AGENT BODY")
    request = build_one_shot_request(delegation=d, effort="low", max_tokens=100, temperature=0.0)

    assert request.system == SYSTEM_PROMPT_ONE_SHOT
    assert "THE AGENT BODY" not in request.system
    rendered = request.messages[0].content[0].text
    assert "THE AGENT BODY" in rendered


def test_the_prompt_order_is_agent_body_then_files_then_task():
    """Order is load-bearing, not cosmetic: the task varies most between calls, so it goes
    last where a changed byte invalidates the least of the cached prefix."""
    rendered = Delegation(task="TASK", files_block="FILES", agent_body="AGENT").render()
    assert rendered.index("AGENT") < rendered.index("FILES") < rendered.index("TASK")
    assert rendered == "AGENT\n\nFILES\n\nTASK"


def test_render_omits_what_is_absent_rather_than_leaving_a_gap():
    assert Delegation(task="TASK").render() == "TASK"
    assert Delegation(task="TASK", agent_body="A").render() == "A\n\nTASK"
    assert Delegation(task="TASK", files_block="F").render() == "F\n\nTASK"


def test_an_empty_task_is_still_refused_by_render():
    """The check moved onto `Delegation` when the two call sites were merged; this is what
    stops the move having quietly dropped it."""
    for empty in ("", "   ", "\n"):
        with pytest.raises(Exception, match="task is empty"):
            Delegation(task=empty, agent_body="A").render()


# --- the two privilege-granting fields ----------------------------------------------------
#
# `network` and `extra_binds` are the only frontmatter that grants something the sandbox
# otherwise withholds: egress, and a read-only mount of a host path. Until 2026-09-05 the
# whole of their validation was a boolean parse and `os.path.isabs`, and two of the three
# lookup tiers sit inside the caller's own workdir -- so a markdown file checked into a
# repository being reviewed could bind any absolute path and switch the network on.
#
# Every refusal below has a companion that must be *accepted*, because a check that refuses
# everything passes a negative test just as well as a correct one does.


def bind_cfg(tmp_path: Path, **over) -> Config:
    """A config whose roots are real paths, so a resolved bind can be compared with one.

    `tmp_path` can carry an 8.3 short name on Windows, which `realpath` expands -- comparing
    an unexpanded root against an expanded bind would fail for a reason that has nothing to
    do with the policy under test.
    """
    root = os.path.realpath(tmp_path / "allowed")
    os.makedirs(root, exist_ok=True)
    kw = {"agent_bind_roots": (root,)}
    kw.update(over)
    return cfg(tmp_path, **kw)


def agent_with(c: Config, name: str, frontmatter: str) -> Path:
    return write(Path(c.agents_dir) / f"{name}.md",
                 f"---\nname: {name}\n{frontmatter}\n---\nbody\n")


@posix_only
def test_an_agent_bind_inside_a_configured_root_is_accepted(tmp_path):
    """The companion to every refusal below. Without it a policy refusing every bind would
    satisfy all of them."""
    c = bind_cfg(tmp_path)
    tools = Path(c.agent_bind_roots[0]) / "tools"
    tools.mkdir()
    agent_with(c, "ok", f"extra_binds: [{tools}]")
    assert agents.load_agent(c, "ok").extra_binds == (str(tools),)


@posix_only
def test_an_agent_bind_outside_every_root_is_refused(tmp_path):
    c = bind_cfg(tmp_path)
    loot = os.path.realpath(tmp_path / "elsewhere")
    os.makedirs(loot)
    agent_with(c, "greedy", f"extra_binds: [{loot}]")
    with pytest.raises(AgentError, match="outside every configured agent bind root"):
        agents.load_agent(c, "greedy")


@posix_only
def test_no_configured_roots_permits_no_bind_at_all(tmp_path):
    """The default. `agent_bind_roots` deliberately does not fall back to `workspace_roots`
    the way `workdir_roots` does, so an unconfigured server grants no mount."""
    c = cfg(tmp_path)
    assert c.agent_bind_roots == ()
    target = os.path.realpath(tmp_path / "anything")
    os.makedirs(target)
    agent_with(c, "any", f"extra_binds: [{target}]")
    with pytest.raises(AgentError, match="no agent bind is permitted at all"):
        agents.load_agent(c, "any")


@posix_only
@pytest.mark.parametrize("target", sorted(base_mount_targets()))
def test_no_agent_bind_can_end_up_shadowing_a_mount_the_sandbox_makes(tmp_path, target):
    """`extra_binds` are emitted after the base mounts, so a bind *at* one replaces it.

    Read-only either way, so what it takes is trust rather than write access -- a
    `/usr/bin/python3` the sandbox did not provide. Fixed here rather than by reordering:
    putting the base mounts last breaks a bind *inside* /tmp, which is every temporary
    directory on Linux and which an integration test caught on 2026-09-05.

    Phrased as the outcome rather than as "this raises", because for four of the nine
    targets it does not and should not: /bin, /lib, /lib64 and /sbin are symlinks into
    usr/, so canonicalising a bind on one lands it *inside* /usr where it shadows nothing.
    Asserting a refusal would have been asserting the wrong thing for those, and asserting
    it only for the other five would need a second hand-kept list.
    """
    c = bind_cfg(tmp_path, agent_bind_roots=("/",))
    agent_with(c, "shadow", f"extra_binds: [{target}]")
    try:
        spec = agents.load_agent(c, "shadow")
    except AgentError as e:
        assert "sits at or above" in str(e)
        return
    for got in spec.extra_binds:
        shadowed = [t for t in base_mount_targets() if path_within_roots(t, (got,))]
        assert not shadowed, f"{target} was accepted as {got}, which still shadows {shadowed}"


@posix_only
def test_a_bind_above_a_mount_the_sandbox_makes_itself_is_refused(tmp_path):
    """The ancestor case. Binding `/` shadows every base mount at once, and a rule phrased
    as equality would let it straight through."""
    c = bind_cfg(tmp_path, agent_bind_roots=("/",))
    agent_with(c, "everything", "extra_binds: [/]")
    with pytest.raises(AgentError, match="sits at or above"):
        agents.load_agent(c, "everything")


@posix_only
def test_a_bind_inside_a_mount_the_sandbox_makes_itself_is_allowed(tmp_path):
    """The companion, and the whole reason the rule is at-or-above rather than overlapping.

    A toolchain under /tmp is the ordinary case -- it is where every temp directory lives --
    and a rule that refused it would be refusing the feature.
    """
    c = bind_cfg(tmp_path, agent_bind_roots=("/tmp",))
    tools = Path("/tmp") / f"agent-bind-{os.getpid()}"
    tools.mkdir(exist_ok=True)
    agent_with(c, "tmptool", f"extra_binds: [{tools}]")
    assert agents.load_agent(c, "tmptool").extra_binds == (str(tools),)


@posix_only
def test_a_bind_at_the_sandbox_home_is_refused(tmp_path):
    """HOME is bound before `extra_binds` too, so it is shadowable in the same way -- but it
    is a config value, so the derived base table cannot know about it."""
    c = bind_cfg(tmp_path, agent_bind_roots=("/",), sandbox_home=str(tmp_path / "sbhome"))
    agent_with(c, "athome", f"extra_binds: [{tmp_path / 'sbhome'}]")
    with pytest.raises(AgentError, match="sits at or above"):
        agents.load_agent(c, "athome")


@posix_only
def test_a_bind_symlinked_out_of_a_root_is_refused_on_where_it_lands(tmp_path):
    """The bind is emitted at a path bwrap resolves at mount time, so a check that read the
    written name would approve this and mount the target anyway."""
    c = bind_cfg(tmp_path)
    loot = Path(os.path.realpath(tmp_path / "elsewhere"))
    loot.mkdir()
    link = Path(c.agent_bind_roots[0]) / "innocent"
    link.symlink_to(loot)
    agent_with(c, "sneaky", f"extra_binds: [{link}]")
    with pytest.raises(AgentError, match="outside every configured agent bind root") as e:
        agents.load_agent(c, "sneaky")
    assert str(loot) in str(e.value), "the refusal must name where it resolved to"


@posix_only
def test_a_bind_symlinked_within_a_root_is_accepted_as_its_resolved_path(tmp_path):
    """Proves the test above is not passing against a policy that refuses every symlink,
    and that the value carried forward is the one that was checked rather than the one that
    was written -- there is no second resolution later to disagree with this one."""
    c = bind_cfg(tmp_path)
    real = Path(c.agent_bind_roots[0]) / "real-tools"
    real.mkdir()
    link = Path(c.agent_bind_roots[0]) / "tools"
    link.symlink_to(real)
    agent_with(c, "linked", f"extra_binds: [{link}]")
    assert agents.load_agent(c, "linked").extra_binds == (str(real),)


def test_network_is_refused_when_the_agent_is_not_named(tmp_path):
    c = cfg(tmp_path)
    agent_with(c, "chatty", "network: true")
    with pytest.raises(AgentError, match="DELEGATE_AGENT_NETWORK_ALLOWED"):
        agents.load_agent(c, "chatty")


def test_a_named_agent_in_the_operators_own_directory_gets_the_network(tmp_path):
    """The companion. Without it every network test would pass against a flat refusal."""
    c = cfg(tmp_path, agent_network_allowed=("chatty",))
    agent_with(c, "chatty", "network: true")
    assert agents.load_agent(c, "chatty").network is True


def test_a_workdir_agent_file_cannot_take_the_network_by_naming_itself(tmp_path):
    """The filed vulnerability, end to end. `_candidates` searches the workspace tiers
    first, so a repository shipping `.claude/agents/<allowlisted-name>.md` shadows the
    operator's own file -- and a grant keyed only on the name would follow the string an
    attacker chose. Egress has no destination list to bound it, so this one is refused on
    provenance rather than narrowed."""
    c = cfg(tmp_path, agent_network_allowed=("chatty",))
    work = tmp_path / "proj"
    write(work / ".claude" / "agents" / "chatty.md",
          "---\nname: chatty\nnetwork: true\n---\nbody\n")
    with pytest.raises(AgentError, match="asks for network: true from outside"):
        agents.load_agent(c, "chatty", str(work))


def test_a_workdir_agent_file_is_still_loadable_without_the_grant(tmp_path):
    """The companion to the one above: provenance gates the network, not the whole tier."""
    c = cfg(tmp_path, agent_network_allowed=("chatty",))
    work = tmp_path / "proj"
    write(work / ".claude" / "agents" / "chatty.md", "---\nname: chatty\n---\nbody\n")
    assert agents.load_agent(c, "chatty", str(work)).network is False
