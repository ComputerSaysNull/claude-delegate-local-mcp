---
name: write-delegate-agent
description: Write an agent file in this server's format, for delegate_to_agent to run, and validate it by calling list_agents. Use when you want a delegation shaped by a reusable role rather than a one-off task.
---

An agent file is a role this server can run: frontmatter that sets the dispatch, and a body
that instructs the model. `delegate_to_agent` and `delegate_to_agent_readonly` take its
name.

This file is itself a valid agent file in the format it describes, so it doubles as the
worked example.

## Where the file goes

Three locations, **first match wins**, so a project's own agent shadows a personal one of
the same name:

1. `<project>/.claude/agents/<name>.md`
2. `<project>/.claude/skills/<name>/SKILL.md`
3. `<agents_dir>/<name>.md` — the personal tier, set by `DELEGATE_AGENTS_DIR`

The **filename supplies the name** — `<name>.md`, or the skill directory for a `SKILL.md`.
A name must match `^[A-Za-z0-9_-]+$`: it is a name, not a path, so it cannot traverse.

## Frontmatter

```markdown
---
name: test-writer
description: Writes and extends pytest tests for a changed module.
model: deepseek-v4-flash
effort: low
max_turns: 20
max_tokens: 32768
keep_tool_results: 6
allowed_tools: [read_file, write_file, run_bash]
network: false
---

You write tests for existing code. Read the module, then write tests that would have
caught the bug described in the task. Run them. Iterate until they pass.

Report the outcome of the final test run.
```

A frontmatter block is **required** — a `---` line, at least one key, a closing `---`. A
file with no block, or with an empty one, is refused. **No individual field is required**,
so the smallest valid file is one key and nothing else.

Every key must be one of these ten. An unknown key is **refused, not ignored**, because a
typo that costs you a setting in silence is the failure this format was rewritten to
prevent:

| Key | Value | Notes |
|---|---|---|
| `name` | the agent's name | Optional, but if present it **must equal the filename**, or the file is refused |
| `description` | one line | What `list_agents` reports, so a caller can choose without opening the file |
| `model` | a model-registry key | Binds the dispatch |
| `effort` | `off`, `low`, `high`, `max` | Refused loudly if misspelt. **Not** `medium` — the backend has no such level. **Not** `inherit` — that is what a *caller* passes to defer to this file |
| `max_turns` | integer ≥ 1 | Above the server's hard cap the file is **refused, not clamped**: a caller's number is transient, a file is committed and trusted |
| `max_tokens` | integer ≥ 1 | Reply budget; the model's own cap still applies afterwards |
| `keep_tool_results` | integer ≥ 0 | How many recent tool results survive history eviction. `0` evicts each one as the next turn starts |
| `allowed_tools` | `[a, b]` | From `read_file`, `search_files`, `read_git`, `run_bash`, `write_file`, `edit_file`. Naming an unimplemented tool is refused. Brackets are the format: bare `a, b` is refused, and so is a `-` block list |
| `network` | `true` / `false` | Default false. An **operator grant**: the agent must also be listed in `DELEGATE_AGENT_NETWORK_ALLOWED` and found in the personal tier |
| `extra_binds` | `[a, b]` | Extra directories visible in the sandbox, within operator-set roots. Read-only, and the denylist still reaches inside. Bracketed like `allowed_tools`, and refused the same way |

Precedence for a value: the call argument, then this file, then the registry row (for
`model` and `effort` only), then the server default.

## The body

Everything below the closing `---` is the role's instructions, stripped of surrounding blank
lines. A body is optional. A `---` inside it is just text: only the opening block is parsed.

It is delivered at the **head of the user message** — body, files, then task — and not as
the system prompt, which is a cached constant nothing per-delegation may enter (ADR-0011).
A body is therefore resent every turn: keep it to standing instructions for the role.

## Validate it

Do not assume the file was read. Call `list_agents` with the same `project` you intend to
delegate with, and find your name: **`agents`** is usable and the only success;
**`skipped`** means it was found and did not parse, with the reason on the entry;
**`other_format`** means it read as a Claude Code agent file, recognised by keys such as
`tools` that this format does not have. Absent from all three means no file was found at
any of the three locations — check the filename against the name, and the `project`.
