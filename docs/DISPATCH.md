<!-- BUDGET: 180 -->
<!-- Split out of ARCHITECTURE.md on 2026-08-27 at 423/425 lines. Sized to leave room for
     M4's turn loop, which is also loop.py and would otherwise land back in the file this
     split relieved. ADR-0032. -->
# Dispatch and the response state machine

What the server sends to a model, and what it does with what comes back. Everything here
is `loop.py` and `backends/`; the orchestration around it -- prefetch, path checks, the
backend cache, the MCP wiring -- is in [ARCHITECTURE.md](ARCHITECTURE.md).

Settings are in [CONFIGURATION.md](CONFIGURATION.md), which is generated, and the decisions
behind these mechanisms have their own record in [../DECISIONS.md](../DECISIONS.md). This
explains the shape they produced.

The turn loop is M4 and not built; this document covers the one-shot path.

## One wire format, behind a seam

Only the OpenAI-compatible adapter ships. But the *canonical* message shape kept
internally is the Anthropic one — content blocks, tool-use and tool-result — because
nothing outside the backend layer then knows which wire protocol is in play. Adding an
Anthropic adapter later is one new file rather than a refactor.

Three conditions keep that true, and breaking any turns it back into a refactor: the
canonical shape stays block-structured and is never flattened to strings; SSE accumulation
lives per adapter behind one contract; model selection is a registry lookup and never a
reintroduced prefix function. (ADR-0008)

Built. The seam holds three things the layer above depends on. Flattening lives in the
adapter and nowhere else, so the canonical side stays block-structured. Failures arrive as
four distinguishable kinds — unreachable, refused with its status *and body* intact so a
specific 400 stays feature-detectable (ADR-0017), a 2xx that is not the promised shape, and
a malformed canonical request, which is our own bug and must never be retried — because
retry is only buildable on that distinction. And `finish_reason` and the token counts come
back **uninterpreted**: null content with `finish_reason: "length"` is a valid response
carrying no text, not an error. Deciding what that means belongs to the response state
machine in M3, which needs the raw value to decide it.

The adapter's single HTTP client bounds **connect and request separately**. One timeout for
both looks harmless, because the failure everyone pictures is a refused connection — and a
refused connection sends RST and fails in milliseconds regardless. A *dropped* route sends
nothing, so a shared bound leaves the connect phase held open for the entire request
timeout. The operating system's own SYN-retransmission limit is the only thing underneath,
and it varies by platform, so the stall is long and its length is not ours to predict.
Connect is therefore bound by its own, much shorter setting.

## Retry sits above the adapter, and honours what the endpoint asks for

The four error kinds exist so retry can be selective, and `loop.py` is where that selection
happens — never in the adapter, which stays a translator. Unreachable is always worth
another attempt; a refusal only for an exact set of statuses, 429 and 500/502/503/504.
Everything else — 400, 401, 403, 404 — describes the request, and sending it again cannot
change the answer. The set is a module constant, not a setting: which codes mean *temporary*
is a fact rather than a preference. A protocol error is never retried. On exhaustion the
last real exception propagates unchanged, because a "gave up after N" wrapper would discard
the very distinction this layer exists to keep.

`Retry-After` is honoured in both spellings RFC 7231 allows — a count of seconds and an
HTTP-date — since which one arrives is the server's choice and reading one honours the
header by luck. An unparseable or absent header falls back to exponential backoff rather
than failing the call: it is a hint from someone else's machine. An honoured delay is used
as sent and deliberately *not* jittered, jitter being for clients that are guessing;
ordinary backoff is jittered across the full range.

Both are capped by [`retry_max_delay`](CONFIGURATION.md), which is not decoration: the wait
sits outside every HTTP call, so no request timeout reaches it and an uncapped header stalls
the delegation as long as it likes. It is deliberately small because two bounds that would
otherwise cover this **do not exist yet** — `dispatch_timeout` is declared and enforced
nowhere, and the per-turn progress notification holding off the client's 30-minute stdio
idle timeout is ADR-0018, arriving with the turn loop. Worst case for one delegation is
`retry_max_attempts` attempts plus the capped waits between them.

The result reports `attempts`: real calls made, counted by the server, while the token
counts beside it describe the attempt that answered rather than the sum. (ADR-0007)

## Reasoning is controlled per request, never inherited

The cluster's reasoning default is set at boot and is not ours to assume — the stack we
run against ships one value in its example environment while two of its own documents
recommend another. So every request states its reasoning level explicitly. Resolution:
agent frontmatter, then the registry row, then the global default.

An unrecognised value is refused rather than passed through — but not because the backend
would ignore it. It does not: it validates `reasoning_effort` and answers a bad one with a
400, measured rather than assumed (JOURNAL 2026-08-26). The refusal here is simply earlier
and cheaper. These are *this project's* levels, not the server's, so the adapter translates
them — `off` is our word for the server's `none` — and a level with no translation would be
refused only after its prefill had been paid for. (ADR-0013)

There is a real failure mode here, reproduced rather than assumed: at high effort with a
small reply budget, reasoning consumes the whole allowance and the response comes back with
null content and a length stop. What the server does about it is below. (ADR-0014)

## An empty answer is recovered from before it is reported

The signature is narrow on purpose: empty text **and** a length stop, together. Either
alone is a different thing — an empty answer at `finish_reason: "stop"` is a model that
genuinely had nothing to say, and a length stop with text in it is ordinary truncation
where an answer exists and is merely cut short. Both must be left alone, because every
mitigation below costs a full generation at the largest budget the model allows.

Three stages, and deliberately not a cascade through every level. Send. If the signature
appears, retry once at the larger of double the budget and
[`thinking_max_tokens_floor`](CONFIGURATION.md), keeping the effort level — this stage
keeps the rendered prompt identical apart from the budget, so the prefix cache survives it.
If that is still empty, step the level down once and send again. The step table is derived
from the level vocabulary rather than written out, so it cannot drift into stepping to a
level that no longer exists.

Stepping down is the last resort rather than the first because it is the expensive one: the
level is part of the rendered prompt, so `prompt_tokens` moves with it (measured, JOURNAL
2026-08-26) and a stepped dispatch misses the prefix cache entirely, paying a fresh prefill
on top of its generation. The stepped budget is resolved again for the new level rather than
inherited, since a lower level no longer needs the headroom the higher one forced up.

One stage is skipped rather than spent: when the model's own cap already pinned the first
budget there is no larger budget to retry at, and re-sending the request unchanged buys a
full generation for a result that cannot differ. That is not a rare edge — high and max
effort already resolve to `thinking_max_tokens_floor`, so where the model cap equals it the
budget stage never fires and the level steps down on the second call. Measured, and the
measurement is why the skip matters rather than being a tidiness: at max effort raising the
budget is not the mitigation at all, and lowering the level is (JOURNAL 2026-08-27, which
also notes what an exhausted max-effort delegation costs in wall clock against the idle
timeout above). A test pins the arithmetic so the stage cannot quietly return.

The two terminal states are different diagnoses, and conflating them would send the caller
to the wrong fix. Still empty after both mitigations is ADR-0014's exhaustion —
`reasoning_exhausted: true`, meaning the task needs more reasoning than this model finishes
inside its budget. Still empty with the level *already* at its lowest is not: there was
nothing left to disable, so the budget was too small for the answer, and reporting that as
exhaustion would tell the caller to lower an effort that is already lowest.

`attempts` counts every real call across all three stages and every transport retry inside
them. The token counts describe the attempt that answered, not the sum: ADR-0014 requires
the retry not to charge the turn budget, and with no turn accounting yet that is what the
rule amounts to here.

