<!-- GENERATED FILE -- do not edit.
     Source: PLAN.md plus git, via scripts/gen_status.py.
     Fixed size by design: current values only, overwritten each run. History
     lives in PLAN.md, CHANGELOG.md and JOURNAL.md. -->

# Status

**Current phase:** M1 — One real backend call — 0 of 5 items done

## In progress

- *nothing marked in progress*

## Next up

- `backends/base.py` — `Backend` protocol, canonical request and response
- `backends/openai_compat.py` — the only adapter shipped
- `delegate()` one-shot, no `files[]` yet

## Progress by phase

| Phase | Done | Active | To do | Cancelled |
| --- | --- | --- | --- | --- |
| M0a — Spikes | 4 | 0 | 0 | 0 |
| M0b — Foundation | 23 | 0 | 0 | 0 |
| M1 — One real backend call | 0 | 0 | 5 | 0 |
| M2 — files[] prefetch | 0 | 0 | 3 | 0 |
| M3 — Response state machine | 0 | 0 | 8 | 0 |
| M4 — Agentic loop and model tools | 0 | 0 | 6 | 0 |
| M5 — Sandbox | 0 | 0 | 7 | 0 |
| M6 — Agents, batching, discovery | 0 | 0 | 3 | 0 |
| M7 — Admission control and polish | 0 | 0 | 4 | 0 |
| Deferred and cancelled | 0 | 0 | 1 | 5 |

**Overall:** 27 done, 37 open, 5 cancelled.

## Repository

- Branch `docs/upstream-review-and-cleanup`, 15 commit(s)
- 49 tracked files, 6 test file(s), 24 decision record(s)
- Working tree: has uncommitted changes

## Recent commits

- `f4c2584` docs: review upstream, and give the review something to live in
- `d08038e` chore: ignore chunked scratch in the repo root
- `ad6f2b4` docs: record the public-surface leak and the repository recreate (#1)
- `2a28636` fix: permit GitHub as a squash-merge committer, never as an author
- `a7a3ae9` feat: scan the pull request title and body
