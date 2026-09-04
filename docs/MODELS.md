<!-- BUDGET: 180 -->
# Models

The registry file, and what adding a model involves.

Server-wide settings are in [CONFIGURATION.md](CONFIGURATION.md). This covers only the
per-model file and its fields.

## Why a file, and not clever naming

The ancestor picked a backend by prefix-matching the model name across five tables. Beyond
being fragile, it cannot express the situation here: **the serving stack runs one model per
inference instance**, so a second model lives at a second base URL — and a URL is not
derivable from a name. One explicit row per model replaces all five tables. (ADR-0009)

There is no name-pattern fallback. A model that merely resembles a registered one is not
quietly routed somewhere plausible; it is refused, and the refusal lists what *is*
registered.

## The file

Copy `models.toml.example` to `models.toml`. It is gitignored, because it names a host, and
a hostname identifies your machine as surely as an address.

```toml
[models.deepseek-v4-flash]
base_url        = "http://YOUR-HEAD-NODE:8888"
served_model_id = "deepseek-v4-flash-0731"
api_format      = "openai"
api_key_env     = ""
context_window  = 1048576
default_effort  = "low"
max_tokens_cap  = 131072
concurrency     = 5
default         = true
```

The table key (`deepseek-v4-flash`) is the handle callers use. It does not have to match
`served_model_id`, and the registry accepts either.

## Fields

| Field | Required | Notes |
|---|---|---|
| `base_url` | yes | **No `/v1` suffix** — the server appends the API path. Including it is refused, with the corrected value in the message |
| `served_model_id` | yes | Exactly as the server reports it at `GET {base_url}/v1/models` |
| `api_format` | no | `openai` (default). `anthropic` parses but has no adapter yet and is refused at load, naming ADR-0008 |
| `api_key_env` | no | *Name* of an environment variable holding a bearer token. Omit when the endpoint needs none. The key itself never appears here |
| `context_window` | no | Used for budgeting headroom. Not enforced against the server, but checked against it when overflow handling is armed — see below |
| `default_effort` | no | `off`, `low`, `high`, `max`. Falls back to the global default |
| `max_tokens_cap` | no | Clamps any larger request. `0` means no cap |
| `concurrency` | no | *This endpoint's* limit, checked alongside the global in-flight cap |
| `default` | no | Exactly one entry may set it, unless a single model is registered or the global default names one |

An unknown field is **refused**, not ignored. A typo in a registry key would otherwise cost
you the setting in silence, which is the class of bug this file exists to remove.

## Choosing `default_effort`

`low` is right for the work this tool exists for. Bulk, mechanical, read-heavy tasks do not
benefit from extended reasoning, and the high settings spend the reply budget on it.

Reproduced against a live cluster: at `max` with a small reply budget, a hard prompt
returned **null content** and a length stop, having spent its whole allowance reasoning.
Even `low` truncated at a small budget — the hazard is not exclusive to the top setting.
ADR-0014 records the measurement.

The server guards this — one retry at a larger budget, then a step down, then an explicit
failure rather than an empty answer — but the guard costs time. Set `low` and raise it per
agent where a task genuinely needs it. (ADR-0013, ADR-0014)

All four levels are supported and none is remapped *downward*: a caller asking for `max`
gets `max`, because an API that disagrees with the backend's own documented values is worse
than one extra enum entry. One level is renamed rather than remapped — `off` is our word for
what the server calls `none` — which changes the spelling and never the strength.

## Adding a second model

On this class of stack a second model is a second container on a second port. There is no
router, and `GET /v1/models` returns one entry per endpoint. So:

```toml
[models.qwen3-vl]
base_url        = "http://YOUR-HEAD-NODE:8889"
served_model_id = "qwen3-vl-4b"
context_window  = 131072
default_effort  = "off"
concurrency     = 2
```

Then either mark one entry `default = true` or set the global default. With several models
and no default the server **refuses to start** rather than choosing: silently picking a
model changes both cost and behaviour.

Note the two concurrency limits are different things and both apply. A registry entry's
`concurrency` bounds that endpoint; the global in-flight cap bounds everything at once.

## Verifying

```bash
curl -s http://YOUR-HEAD-NODE:8888/v1/models | python3 -m json.tool
```

`served_model_id` must match the `id` field exactly. The server probes this same path for
health — deliberately, because bare vLLM does **not** serve the `/health/liveliness` the
ancestor probed, which belongs to a proxy it used to sit behind. Probing the wrong path
made a healthy cluster look unreachable.

If the endpoint is behind an overlay VPN or similar, check that it resolves from **inside**
the environment running the server, *and to the same address the host resolves it to*.
Resolution succeeding in the guest is not the same as resolving correctly. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Failure messages

Every malformed registry is refused at startup, never at first use, and the message names
the fix. Covered by tests: unknown field, a `/v1` suffix, a missing scheme, a missing
required field, a bad effort value, an unimplemented format, two defaults, several models
with no default, zero concurrency, invalid TOML, and a missing file.

A bad registry discovered thirty minutes into a delegation is a far worse experience than a
refusal to boot.

## The window is checked, never derived

`context_window` is yours to set and nothing overrides it. When `context_overflow_enabled`
is on, the server asks the endpoint once per model what window it is actually serving and
compares the two — because every overflow threshold is a share of this number, and a
threshold over a wrong denominator is the bug that feature exists to avoid.

A disagreement disarms overflow handling for that model and reports both numbers. The
endpoint's figure is never adopted: an auto-derived window is how the ancestor project came
to compute every threshold against a model file's architecture maximum rather than the
window being served. An endpoint that reports no window is fine and blocks nothing; vLLM
reports one as `max_model_len` (JOURNAL 2026-08-29).

An entry that omits `context_window` gets the default, and the server now records that it
did. It was silent until 2026-09-02 — the local form of the same bug — and the cost was a
mismatch report that told an operator their file *gives* a number it does not mention
anywhere, advice to correct a line that was never there. The report now says the window was
assumed and names the one the endpoint served, and `backend_status` carries
`context_window_defaulted` beside the number, so the two cases are distinguishable before
anything disagrees. Overflow handling stays off by default regardless: arming it against a
number nobody chose is worse than not arming it.

## The token estimator is measured against a tokenizer, not against file types

The prefetch and admission budgets are denominated in estimated tokens, from a
per-extension bytes-per-token table in `config.py`. Those ratios are a property of the
**tokenizer**, not only of the file type, and the spread between two models is as wide as
the spread between JSON and prose: measured against one tokenizer and then another, JSON
moved by 47% while Python source did not move at all. The numbers themselves live in
JOURNAL 2026-08-25 and 2026-09-04 and are deliberately not repeated here or in
`config.py`; ADR-0019 has the reasoning.

What this means when you add a model: the table is **not** re-derived per registry entry,
and there is no per-model field for it. It survives a change of tokenizer only because
every entry is rounded down from its measurement, so the estimator over-counts — and
over-counting costs a little admission capacity while under-counting queues a request
until it times out. A tokenizer denser than the densest entry would break that, silently,
because nothing compares the table against the model being served.

So if a new model is much denser than the one the table was built against, re-measure
before trusting the budgets. The endpoint's own `/tokenize` is what both measurements
used, which makes this a half-hour job rather than a project.
