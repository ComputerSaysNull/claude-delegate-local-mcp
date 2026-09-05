"""Every tunable in this project, in one place.

This module is the **single source of truth** for configuration. `docs/CONFIGURATION.md`
is generated from the field metadata below by `scripts/gen_config_docs.py`, and the docs
gate fails if the generated file drifts from this code. That is deliberate: the ancestor
project documents one setting as three different values in three places (README, config
reference, and the code itself), and this is the mechanism that makes that impossible
here. See ADR-0004.

So: never state a default anywhere else. Not in a docstring, not in a README, not in a
comment in another module. Add the field here with a `description`, run the generator.

Environment prefix is `DELEGATE_`. It was `DEEPSEEK_DELEGATE_` while this server spoke to
exactly one model; ADR-0009 made the model registry explicit and multi-model, so a
model-specific prefix became wrong.

Not a port -- this file is new. The ancestor kept its configuration as ~40 loose module
constants with two inconsistent prefixes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# Values accepted by *this project* for reasoning effort -- deliberately not the server's
# set, which has its own vocabulary. `backends/openai_compat.py` owns the translation, and
# docs/ARCHITECTURE.md says why validating here is not redundant. See ADR-0013.
EFFORT_LEVELS = ("off", "low", "high", "max")

# The fifth thing a *caller* may say, and deliberately not a fifth level. The tool argument
# is required, so there is no longer an absent value for the precedence chain to fall
# through on; this is how a caller states that it is deferring to the agent file, then the
# registry row, then `thinking_default`. Normalised to `None` at the tool boundary, so
# nothing inside the server ever sees it -- internally "no explicit effort" is still spelled
# `None`, and `resolve_effort` still refuses this string, correctly: reaching it means the
# boundary was bypassed. Never valid for `default_effort` or `thinking_default`, which are
# the ends of that chain and have nothing left to defer to. See ADR-0045.
EFFORT_INHERIT = "inherit"

# The transports this server implements. One, and the tuple stays a tuple so the
# refusal below has something to name and a second entry is one line to add.
TRANSPORTS = ("stdio",)

# The client's stdio idle timeout, in seconds. Not ours to set and not a setting: it is a
# property of Claude Code, measured on 2026-09-01 rather than read off a document. At this
# many seconds of silence the client abandons the tool call and **nothing reaches the
# server** -- no cancellation, no EOF -- so the dispatch runs on holding its admission slot
# until it finishes on its own. The server cannot detect the condition; only sending
# something prevents it, which is what makes the interval below a correctness setting.
# (JOURNAL 2026-09-01, docs/DISPATCH.md)
CLIENT_STDIO_IDLE_TIMEOUT = 1800

# Fractions of the model's context window at which the agentic loop changes behaviour:
# tighten retention, nudge the model to wrap up, then abort. Constants rather than
# settings, for the same reason EFFORT_LEVELS is one -- they name three points on a single
# escalation, and an operator free to move them independently can invert the order so the
# loop aborts before it has ever nudged. The reserve beside them IS a setting, because it
# is a size rather than a stage.
OVERFLOW_TIGHTEN_AT = 0.70
OVERFLOW_NUDGE_AT = 0.85
OVERFLOW_ABORT_AT = 0.95

# Bytes per token, MEASURED (ADR-0019). These vary by more than 2x across file types,
# which is why the prefetch budget is denominated in tokens rather than bytes -- 128 KiB
# is 33K tokens of Python but 72K tokens of JSON.
#
# A ratio here belongs to a file type AND a tokenizer, which the first version of this
# comment did not say. Re-measured against a second tokenizer on 2026-09-04, JSON moved
# 47% while Python source did not move at all, so the model matters as much as the
# extension. The measurements live in JOURNAL 2026-08-25 and 2026-09-04 and are not
# repeated here; the values below are those numbers rounded down, which is a different
# thing.
#
# Each entry is rounded DOWN from the measurement, so an estimate errs toward
# over-counting: over-counting wastes a little admission capacity, under-counting queues
# a request until it times out. The unknown-extension default is the worst case observed.
#
# That convention is also the only reason the table survived the second tokenizer, and it
# would not survive one denser than the densest entry: nothing compares this table against
# the model actually being served. `.toml` and `.lock` share an entry sized for the
# lockfile, so TOML source is over-costed by about 45% -- safe direction, real cost.
BYTES_PER_TOKEN: dict[str, float] = {
    # structured / punctuation-dense: many tokens per byte
    ".json": 1.7, ".lock": 2.0, ".toml": 2.0, ".yaml": 2.2, ".yml": 2.2,
    ".csv": 1.8, ".tsv": 1.8, ".xml": 2.0, ".html": 2.4, ".css": 2.6,
    # prose
    ".md": 3.3, ".rst": 3.3, ".txt": 3.3,
    # code
    ".py": 3.7, ".pyi": 3.7, ".ts": 3.4, ".tsx": 3.4, ".js": 3.4, ".jsx": 3.4,
    ".mjs": 3.4, ".rs": 3.5, ".go": 3.5, ".java": 3.5, ".kt": 3.5, ".c": 3.5,
    ".h": 3.5, ".cpp": 3.5, ".hpp": 3.5, ".cs": 3.5, ".rb": 3.4, ".php": 3.4,
    ".swift": 3.5, ".lua": 3.4, ".sh": 3.2, ".sql": 3.2, ".ini": 2.6, ".cfg": 2.6,
}

# An unknown extension is costed at the densest ratio in the table, DERIVED rather than
# written down again. A hardcoded fallback drifted the moment a denser entry was added:
# the default said 1.8 while .json was already 1.7, so "worst case" was not the worst.
BYTES_PER_TOKEN_DEFAULT = min(BYTES_PER_TOKEN.values())

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _f(default: Any, description: str, *, unit: str = "") -> Any:
    """Declare a config field with the metadata the doc generator reads."""
    return field(default=default, metadata={"description": description, "unit": unit})


class ConfigError(ValueError):
    """Raised at startup for a malformed or missing setting.

    Always fails at load time rather than at first use: a bad timeout discovered
    thirty minutes into a delegation is a much worse experience than a refusal to boot.
    """


@dataclass(frozen=True, slots=True)
class Config:
    # ---- backend selection -------------------------------------------------------
    models_file: str = _f(
        "./models.toml",
        "Path to the model registry. Each entry carries its own base URL, API format, "
        "key variable, context window and defaults -- see docs/MODELS.md.",
    )
    default_model: str = _f(
        "",
        "Registry key used when a call names no model. Empty means use the entry marked "
        "`default = true`, and fail at startup if exactly one is not so marked.",
    )

    # ---- path policy, layers 1 to 4 (ADR-0006) -----------------------------------
    workspace_roots: tuple[str, ...] = _f(
        (),
        "REQUIRED. Layer 1: directories a delegated model may read from, separated by "
        "os.pathsep. Any path whose real location falls outside every root is refused, "
        "which is what closes symlink escapes. Written in native host form.",
    )
    workdir_roots: tuple[str, ...] = _f(
        (),
        "Layer 1 applied to the `workdir` argument itself, which is a separate surface "
        "from the files read within it. Empty means reuse workspace_roots.",
    )
    ext_allowlist: tuple[str, ...] = _f(
        (
            ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json",
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", ".html", ".sql", ".sh",
            ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb",
            ".php", ".swift", ".lua", ".ini", ".cfg", ".env-example", ".gitignore",
            ".dockerfile", ".makefile",
        ),
        "Layer 2: the practical allowlist. A pure allowlist cannot work for file "
        "contents -- you cannot enumerate every source file you will ever delegate -- so "
        "extension is the axis that can be allowlisted. Anything not listed is refused.",
    )
    secret_globs_file: str = _f(
        "./security/secret_globs.txt",
        "Layer 3: globs a model must never receive, shared with the git secrets gate so "
        "there is one list and not two that drift.",
    )
    respect_gitignore: bool = _f(
        True,
        "Layer 4: refuse paths git ignores. Cheap, and catches build output and local "
        "environment files that pass the extension allowlist.",
    )

    # ---- files[] prefetch (ADR-0015, revised by ADR-0019) ------------------------
    max_file_tokens: int = _f(
        140000,
        "Per-file prefetch cap, in ESTIMATED TOKENS rather than bytes. Bytes were the "
        "wrong unit: the same byte limit is worth 2x more tokens for JSON than for "
        "Python. A file over the cap is skipped whole, never truncated -- source cut "
        "mid-function is worse than absent, because the model will confidently repair "
        "code it never saw. Equal to max_total_prefetch_tokens by default, so no file is "
        "dropped for being large while the budget it would have fitted in sits unused "
        "(ADR-0046): the largest documents are the ones most likely to have drifted, and "
        "a cap that removes them is a cap that removes what an audit came for. Lower it "
        "to refuse one huge file while still allowing a large total; that is the only "
        "job it has left, because fairness between concurrent requests belongs to "
        "admission control, which counts it across every process on the machine.",
        unit="est. tokens",
    )
    max_total_prefetch_tokens: int = _f(
        140000,
        "Total files[] budget per call, in estimated tokens. Measured prefill runs "
        "1900-2600 tok/s, so this is about 55s before the model says a word -- paid once "
        "per distinct prefix, since the cluster caches prefixes and the prompt is "
        "ordered to keep them stable. Well inside a 1M window, and small enough that one "
        "request cannot monopolise a shared KV pool.",
        unit="est. tokens",
    )
    max_file_read_bytes: int = _f(
        4194304,
        "Hard byte ceiling checked by stat() BEFORE reading, so a multi-gigabyte file is "
        "never loaded into memory just to discover it is too big. Not a context budget -- "
        "that is max_file_tokens.",
        unit="bytes",
    )

    # ---- model-facing tool limits ------------------------------------------------
    search_max_files_scanned: int = _f(
        2000,
        "Files search_files will open before it stops looking and says so. Not a "
        "correctness bound -- the path policy is that -- but a wall-clock one: the "
        "workspace lives on /mnt/c, which is roughly 12x slower per file than ext4 "
        "(ADR-0020), so an unbounded walk of three project roots is a delegation spent "
        "on directory traversal. The result says when the cap was reached, because a "
        "truncated search that reads like an exhaustive one is worse than a refusal.",
    )
    max_read_chars: int = _f(
        50000,
        "Cap on one read_file response. The model is told the true total and how to page, "
        "so it continues by range rather than re-reading.",
        unit="chars",
    )
    max_write_bytes: int = _f(
        8388608,
        "Cap on one write_file call, and on the result of one edit_file call.",
        unit="bytes",
    )
    run_bash_timeout: int = _f(
        600,
        "Per-command timeout for run_bash. Sized to tell a hung command from a slow one, "
        "which means it has to sit above the slowest legitimate command rather than near "
        "it. Running a project's test suite is the first thing a delegated model is asked "
        "to do once it has a workdir, and this repository's own suite takes 281s serially "
        "in WSL -- so 120s, the previous value, was below the median legitimate command and "
        "killed real work. A kill is reported as a non-zero exit, and the model then "
        "reasons about it as a test failure and 'fixes' passing code, which corrupts the "
        "ground truth the whole self-verification design rests on (ADR-0007). The opposite "
        "error only wastes wall clock, and dispatch_timeout bounds it anyway.",
        unit="seconds",
    )
    max_bash_output_chars: int = _f(
        20000,
        "Cap on the combined stdout and stderr of one run_bash call. The tail is cut "
        "and the true length stated, never silently dropped -- a build log is exactly "
        "the kind of output whose last line matters most.",
        unit="characters",
    )

    # ---- generation budgets ------------------------------------------------------
    max_tokens: int = _f(
        65536,
        "Cap on tokens generated in a single reply. Reasoning counts against this, which "
        "is the whole mechanism behind the empty-answer failure -- see ADR-0014.",
        unit="tokens",
    )
    thinking_default: str = _f(
        "low",
        "Reasoning effort when neither the agent nor the registry entry specifies one. "
        f"One of {EFFORT_LEVELS}. Sent explicitly on every request rather than inherited, "
        "because the cluster's own default is set at boot and is not ours to assume.",
    )
    thinking_max_tokens_floor: int = _f(
        131072,
        "Floor applied to max_tokens when effort is high or max, and the size retried "
        "after an empty answer. Admission accounting must use the retry's size.",
        unit="tokens",
    )
    resend_reasoning: bool = _f(
        False,
        "Send the model's prior reasoning back as history. Off: it costs input tokens and "
        "prefill on every turn, the conclusions already survive in the visible answer, and "
        "a growing prefix defeats prefix caching.",
    )
    tool_call_temperature: float = _f(
        0.2,
        "Temperature for every turn of the agentic loop. Low because tool-call syntax "
        "tokens are sampled at the request temperature, so malformed calls grow likelier "
        "as it rises. The one-shot path uses one_shot_temperature instead.",
    )
    one_shot_temperature: float = _f(
        1.0,
        "Temperature for the one-shot delegate() path. Separate from "
        "tool_call_temperature because that value is low to protect tool-call syntax, and "
        "the one-shot path emits no tool calls -- there is no syntax to protect and "
        "nothing to gain from suppressing the model's own default sampling.",
    )

    # ---- the agentic loop --------------------------------------------------------
    max_turns_default: int = _f(
        25,
        "Round trips a delegation gets before the server stops it. One turn is one model "
        "reply plus any tool it ran.",
    )
    max_turns_hard_cap: int = _f(
        100,
        "Ceiling no agent file or caller may exceed. Stops an agent definition asking for "
        "500 turns and occupying the cluster for hours. Raised from 40 once max_turns "
        "became overridable per call: the cap exists to refuse an absurd number, not to "
        "decide what a hard case may ask for, and 40 was low enough to be the second. A "
        "caller's number is clamped here silently; an agent file's is refused, so the "
        "file is fixed rather than quietly ignored.",
    )
    keep_tool_results: int = _f(
        6,
        "Most recent tool results kept intact; older ones collapse to a one-line stub. "
        "Every turn resends the whole history, so this is what stops quadratic growth.",
    )

    # ---- context overflow (M4) ---------------------------------------------------
    context_overflow_enabled: bool = _f(
        False,
        "Master switch for both halves of overflow handling: the retroactive check that "
        "notices a prompt has stopped growing while the history did not, and the "
        "preventive response that tightens retention, nudges and finally aborts as "
        "projected usage climbs. Off by default because every threshold here is measured "
        "against ModelEntry.context_window, and a registry entry that omits that field "
        "inherits a silent default -- arming this against a window nobody set would "
        "compute every threshold from a number the operator never chose.",
    )
    overflow_plateau_slop_tokens: int = _f(
        64,
        "Tolerance absorbed before a prompt that did not grow counts as one that could "
        "not. Without it a backend trimming a token or two between turns reads as "
        "truncation, and the delegation aborts on jitter.",
        unit="est. tokens",
    )
    overflow_min_growth_tokens: int = _f(
        256,
        "History growth in one turn below which a plateau is evidence of nothing. Sized "
        "from measurement, not taste: a nine-word message costs about 100 prompt tokens "
        "of chat-template envelope (JOURNAL 2026-08-29), so a threshold in the tens would "
        "fire on the envelope alone. A real tool result is far above this.",
        unit="est. tokens",
    )
    overflow_reserve_fraction: float = _f(
        0.05,
        "Headroom held back for the reply in every projected-usage figure, as a FRACTION "
        "of the model's context window. A fraction rather than a flat count on purpose: a "
        "flat reserve large enough to matter for a 1M window exceeds 95% of an 8K one on "
        "its own, so the detector would report a small model as full at rest. Scaling it "
        "closes that whole bug class instead of picking a number that happens to suit "
        "both.",
    )
    overflow_probe_cache_ttl: float = _f(
        900.0,
        "How long a failed window check is remembered before it is asked again. The point "
        "of the expiry is the whole setting: upstream cached this verdict forever, so a "
        "single transient outage disabled overflow handling until someone restarted the "
        "server. Fifteen minutes is long enough that a dead endpoint is not re-probed on "
        "every delegation, and short enough that a recovered one arms itself without "
        "intervention.",
        unit="seconds",
    )
    overflow_tightened_keep_tool_results: int = _f(
        1,
        "Replaces keep_tool_results once projected usage passes the first threshold, for "
        "the rest of the delegation. Retention only tightens; it never relaxes again, "
        "because a delegation that recovers headroom by evicting has not stopped growing.",
    )

    # ---- timeouts ----------------------------------------------------------------
    turn_timeout: int = _f(1800, "Per-turn backend call timeout.", unit="seconds")
    connect_timeout: int = _f(
        30,
        "Bound on the TCP-connect phase alone, separate from turn_timeout. A refused "
        "connection sends RST and fails in milliseconds without this, but a dropped or "
        "blackholed route sends nothing and would otherwise stall for the whole of "
        "turn_timeout before httpx gives up.",
        unit="seconds",
    )
    status_probe_timeout: int = _f(
        10,
        "Deadline for one backend_status() probe of /v1/models. Separate from, and far "
        "below, turn_timeout: a status check is answered from memory and returns in "
        "milliseconds, so waiting a generation-sized budget on it only means one "
        "blackholed endpoint stalls the report on every other one.",
        unit="seconds",
    )
    dispatch_timeout: int = _f(
        14400,
        "Whole-delegation ceiling, spanning every retry and every empty-answer recovery "
        "stage. An absolute bound on work that is still producing, and nothing more: a "
        "delegation that has STOPPED producing is bounded by stall_timeout instead, far "
        "below this. Raised from 3600 once that existed (ADR-0047). The old value was "
        "sized against Claude Code's 30-minute stdio idle timeout, which ADR-0018's "
        "per-turn notification stopped letting bind, and nothing re-derived it when that "
        "changed -- so an hour was the price of a wedged run, twice over at the default "
        "turn_timeout. This bounds the wait, not the client's patience.",
        unit="seconds",
    )
    stall_timeout: int = _f(
        2100,
        "No-progress deadline: how long a delegation may run without COMPLETING a turn. "
        "Distinct from dispatch_timeout, which bounds total time and cannot tell a "
        "delegation that is merely long from one that is wedged -- the case that needs "
        "killing from the case that must not be. The default sits just above turn_timeout "
        "on purpose: it permits one full-length attempt and refuses a second with nothing "
        "to show, which is the exact shape of the failure that prompted it. Refused below "
        "turn_timeout or above dispatch_timeout -- at or below the former it cuts short a "
        "call that was merely slow, and above the latter it can never fire. A one-shot "
        "never completes a turn, so this is measured from its start and becomes its "
        "effective bound (ADR-0047).",
        unit="seconds",
    )
    keepalive_interval: int = _f(
        60,
        "How often a one-shot delegation reports that it is still running, to the client "
        "as a progress notification and to the transcript as an `alive` event. The loop "
        "reports once per turn (ADR-0018) and a one-shot has no turns, so without this it "
        "is silent for its whole duration -- which is the one call shape that can still "
        "reach the client's stdio idle timeout and be abandoned while working. That "
        "abandonment was measured on 2026-09-01: nothing reaches the server, so it keeps "
        "the admission slot until the work ends on its own. Refused at startup above half "
        "the idle timeout for that reason. It is not a deadline, and nothing is cancelled "
        "when the interval passes.",
        unit="seconds",
    )
    retry_max_attempts: int = _f(3, "Attempts on a retryable backend status.")
    retry_base_delay: float = _f(1.0, "Exponential backoff base.", unit="seconds")
    retry_max_delay: float = _f(
        20.0,
        "Cap on a single wait between attempts, including one the endpoint asked for via "
        "Retry-After. Uncapped, a large or hostile Retry-After stalls a call far past "
        "anything turn_timeout was meant to bound, and the wait happens between requests "
        "where no HTTP timeout applies to it. Kept well under the stdio idle timeout "
        "even though ADR-0018's notification now holds that off: it fires once at the top "
        "of a turn, and this wait sits inside one, unobserved.",
        unit="seconds",
    )

    # ---- admission control (ADR-0012) -------------------------------------------
    max_inflight_seqs: int = _f(
        5,
        "Total concurrent backend requests across all models. Distinct from a registry "
        "entry's own `concurrency`, which caps one endpoint; both are checked.",
    )
    kv_token_budget: int = _f(
        2400000,
        "Summed estimated live tokens permitted in flight. Sits just under the measured "
        "KV pool. Exceeding the pool queues rather than failing, so this protects latency "
        "rather than correctness.",
        unit="tokens",
    )
    large_prefill_tokens: int = _f(
        32768,
        "A request estimated above this counts as a large cold prefill.",
        unit="tokens",
    )
    max_inflight_large_prefills: int = _f(
        2,
        "Concurrent large prefills. The engine admits one long prefill at a time, so this "
        "is one running plus one staged -- pipelining, not throttling. Raising it makes "
        "every large request slower rather than any of them faster.",
    )
    admission_wait_timeout: int = _f(
        1800,
        "Bound on time spent waiting for a slot, before dispatch_timeout starts its own "
        "clock. Separate rather than shared, because dispatch_timeout's deadline is set "
        "inside the loop it bounds, which does not run until admission has already been "
        "granted -- it cannot bound a wait that ends before it begins. The two therefore "
        "stack rather than divide one budget, so the caller-visible worst case is both "
        "added. Raised from 600 once admission started queueing rather than merely "
        "waiting: until then a waiter had no place in line, so a longer timeout bought a "
        "longer unfair wait and risked starvation instead of bounded failure. A wait is "
        "now the work ahead of you, which a busy cluster can legitimately make minutes "
        "long -- where 600s refused requests that would have run. Still far under "
        "dispatch_timeout, and safe against the client's stdio idle timeout only because "
        "a waiting delegation keeps reporting progress. backend_status reports the "
        "longest wait seen, how many hit this limit, and how deep the queue is.",
        unit="seconds",
    )

    cross_process_slots: bool = _f(
        True,
        "Count the four admission rules against every server process on this machine "
        "rather than against this one alone. On by default because the default transport "
        "is stdio, which gives each connected client its own server process: with this "
        "off, every rule above bounds one editor window, and the cluster sees the "
        "configured limit multiplied by however many windows are open. Turning it off is "
        "only correct where this really is the sole process against the endpoint. Needs a "
        "POSIX filesystem lock, so it is inert on Windows -- backend_status reports "
        "whether it is actually active, and never assumes it is. See ADR-0040.",
    )
    slots_dir: str = _f(
        "",
        "Directory holding the shared admission counters. Empty means XDG_RUNTIME_DIR, "
        "falling back to /dev/shm -- both tmpfs, which is what makes losing the file on "
        "reboot correct rather than lossy. Set it only to separate installations that "
        "must not share a budget, such as two checkouts pointed at genuinely different "
        "clusters; two projects sharing one cluster must share one directory, which is "
        "the default and needs no configuration. Never put this on /mnt/c: locking "
        "across the Windows drive boundary is not dependable (ADR-0020).",
    )

    opaque_globs_file: str = _f(
        "./security/opaque_globs.txt",
        "Directories covered and not walked, because they hold machine-generated bulk. "
        "Separate from the secret denylist and matched the same way: a hit is covered with "
        "the tmpfs a matched secret directory gets, so covering more is never less safe -- "
        "what the list buys is the walk, which runs per run_bash call. Unlike the secret "
        "denylist a missing file is not fatal, because an empty list only costs time. "
        "Never list a directory a sandboxed command needs to read (ADR-0041).",
    )

    # ---- operator transcript (ADR-0024) ------------------------------------------
    transcript_dir: str = _f(
        "",
        "Directory to write one record per dispatch into. Empty disables it, and empty "
        "is the default. Independent of the caller's `diagnostics` argument by design: "
        "what an operator can audit should not depend on what the calling session "
        "thought to ask for. Setting it must not change a single byte of any response. "
        "Records carry the task, the files and their accounting, real token usage and "
        "the server-captured ledger -- but never file contents, which are recoverable "
        "from the repository by path and are the only bulky part.",
    )

    # ---- sandbox (ADR-0010) ------------------------------------------------------
    bwrap_bin: str = _f("bwrap", "bubblewrap binary name or path.")
    prlimit_bin: str = _f(
        "prlimit",
        "util-linux prlimit binary name or path. Used only when a sandbox resource limit "
        "is set; if one is and this is missing, run_bash is refused rather than run "
        "unbounded.",
    )
    sandbox_home: str = _f(
        "~/.cache/claude-delegate-local/sandbox-home",
        "Persistent HOME inside the sandbox. The real HOME is never bound, so credential "
        "directories are absent rather than merely unwritable.",
    )
    toolchain_binds: tuple[str, ...] = _f(
        (),
        "Extra read-only binds so tools resolve inside an empty root. Empty means probe "
        "for `uv` and bind it: it lives outside the sandbox HOME, so `uv run pytest` fails "
        "with 'not found' without this. The single most likely first-run sandbox failure.",
    )
    env_passthrough: tuple[str, ...] = _f(
        (),
        "Extra environment names allowed through to a sandboxed command, on top of the "
        "built-in allowlist.",
    )
    secret_shadow_max_entries: int = _f(
        10000,
        "Entries the mount-level secret scan may visit before it gives up and refuses the "
        "command. Also a latency ceiling: the scan runs per run_bash call, and the "
        "workspace lives on /mnt/c, where a walk costs roughly 6ms per entry. This "
        "repository scans in 248 with the opaque list applied, and in 10586 without it -- "
        "0.7 seconds against 66. Raising this is almost never the right answer to a "
        "refusal; naming the bulky directory in the opaque list is (ADR-0041).",
    )
    secret_shadow_max_depth: int = _f(
        24,
        "Directory depth the mount-level secret scan may descend before it gives up and "
        "refuses the command. Guards against a symlink loop the walk cannot see.",
    )
    sandbox_max_memory_mb: int = _f(
        8192,
        "Address space one sandboxed process may reserve, applied as RLIMIT_AS. 0 removes "
        "the limit. Address space is not resident memory, so this over-counts: a Go "
        "runtime or a sanitiser reserves far more than it uses and will need this raised "
        "or turned off. It is the approximation available, because the accurate control is "
        "a cgroup memory limit and no unprivileged process here can set one.",
        unit="MB",
    )
    sandbox_max_file_mb: int = _f(
        2048,
        "Largest file a sandboxed command may write, applied as RLIMIT_FSIZE. 0 removes "
        "the limit. Exceeding it kills the writer with SIGXFSZ and leaves the file "
        "truncated at exactly this size.",
        unit="MB",
    )
    sandbox_max_processes: int = _f(
        512,
        "Concurrent processes the sandbox's user may hold, applied as RLIMIT_NPROC. 0 "
        "removes the limit. Counted per real uid across the machine rather than per "
        "sandbox, so concurrent delegations share this budget and a fork bomb in one "
        "starves the others -- which is still the better failure. Below roughly 64 bwrap "
        "cannot create its namespaces at all and every command fails at startup; when the "
        "cap does bind, the shell cannot fork to report it, so the command dies with no "
        "output rather than a message.",
    )
    sandbox_tmpfs_mb: int = _f(
        1024,
        "Size of the sandbox's /tmp, applied as bwrap's --size. 0 leaves it unbounded, "
        "where filling it is filling RAM. A write that overruns it fails with ENOSPC, "
        "which is an ordinary error a command can report.",
        unit="MB",
    )

    # ---- agents ------------------------------------------------------------------
    agents_dir: str = _f(
        "~/.claude/agents",
        "Third and last place an agent definition is looked for, after the workspace's "
        "own agents and skills directories.",
    )
    agent_bind_roots: tuple[str, ...] = _f(
        (),
        "Roots an agent file's extra_binds may resolve inside, separated by os.pathsep. "
        "Empty means no agent-supplied bind is ever permitted. Deliberately does not fall "
        "back to workspace_roots the way workdir_roots does: those roots are sized for "
        "showing a file's contents, not for granting a mount, and the two of them being "
        "the same list is how one widened for a reading tool would silently widen this. "
        "Written as sandbox-side POSIX paths, because that is what a bind is. An "
        "operator's own toolchain_binds are not checked against this.",
    )
    agent_network_allowed: tuple[str, ...] = _f(
        (),
        "Names of agents permitted to ask for network egress, separated by os.pathsep. "
        "Empty means none, and an agent naming network: true is refused rather than "
        "quietly run without it. Being named here is necessary and not sufficient: the "
        "file must also have been found in agents_dir, since a repository can ship a "
        "workspace-tier agent file under any name it likes.",
    )

    # ---- transport ---------------------------------------------------------------
    transport: str = _f(
        "stdio",
        f"One of {TRANSPORTS}, and anything else is refused at load rather than starting "
        "a server. Adding the HTTP transport is a real integration task, not a flag flip: "
        "session handling and content serialisation differ, and nothing here issues or "
        "checks a token, so it would serve unauthenticated. Kept as a setting, unlike "
        "ADR-0034's sandbox_enabled, because naming another transport should be an error "
        "rather than silence -- load() reads only variables matching a field, so deleting "
        "this one would make a stale value do nothing without saying so.",
    )

    # ---------------------------------------------------------------------------
    def _check_deadlines_nest(self) -> None:
        """The deadlines have to nest: connect <= turn < stall <= dispatch.

        Together rather than scattered through `__post_init__`, because the chain is the
        point. Each check reads as arbitrary alone; in sequence they say that every
        deadline is bounded by the one containing it, and that a reader can see where a
        new one would have to fit. Adding `stall_timeout` was what made that worth
        extracting -- it belongs strictly between two existing links.
        """
        if self.connect_timeout > self.turn_timeout:
            raise ConfigError(
                f"DELEGATE_CONNECT_TIMEOUT ({self.connect_timeout}) exceeds "
                f"DELEGATE_TURN_TIMEOUT ({self.turn_timeout}): the connect phase cannot "
                "be allowed to outlast the whole call it is part of."
            )

        if self.turn_timeout > self.dispatch_timeout:
            raise ConfigError(
                f"DELEGATE_TURN_TIMEOUT ({self.turn_timeout}) exceeds "
                f"DELEGATE_DISPATCH_TIMEOUT ({self.dispatch_timeout}): a single turn "
                "could outlive the delegation containing it."
            )

        if not self.turn_timeout <= self.stall_timeout <= self.dispatch_timeout:
            raise ConfigError(
                f"DELEGATE_STALL_TIMEOUT ({self.stall_timeout}) must be at least "
                f"DELEGATE_TURN_TIMEOUT ({self.turn_timeout}) and no higher than "
                f"DELEGATE_DISPATCH_TIMEOUT ({self.dispatch_timeout}). Below the turn "
                "timeout it would cut short one legitimately slow backend call and report "
                "a stall where there is none; above the ceiling it could never fire.\n"
                "Not a strict lower bound, deliberately: turn_timeout may equal "
                "dispatch_timeout, which the check below permits, and a strict one would "
                "leave that configuration with no legal value at all."
            )

    def __post_init__(self) -> None:
        if not self.workspace_roots:
            raise ConfigError(
                "DELEGATE_WORKSPACE_ROOTS is required and has no safe default. It is "
                "layer 1 of the path policy: without it, nothing bounds which files a "
                "delegated model may read. Set it to the directory holding your projects."
            )
        if self.thinking_default not in EFFORT_LEVELS:
            raise ConfigError(
                f"DELEGATE_THINKING_DEFAULT={self.thinking_default!r} is not one of "
                f"{EFFORT_LEVELS}. Refused at load: these are this project's levels, "
                "which the backend adapter translates to the server's own vocabulary. An "
                "unlisted one has no translation."
            )
        if self.transport not in TRANSPORTS:
            # Refused, not degraded to stdio with a warning. A knob advertised as
            # unfinished that still starts a server is the shape ADR-0034 deleted
            # `sandbox_enabled` for, and an HTTP listener here would be unauthenticated:
            # measured, FastMCP defaults its host to loopback and only a port was ever
            # passed, so the reachable surface was other local processes rather than the
            # network -- but no token is the true half of that finding.
            raise ConfigError(
                f"DELEGATE_TRANSPORT={self.transport!r} is refused; this server "
                f"implements {TRANSPORTS}. The HTTP transport is a real integration task "
                "rather than a flag flip -- session handling and content serialisation "
                "differ -- and nothing here issues or checks a token, so it would serve "
                "unauthenticated. Starting a listener on that basis is worse than "
                "refusing to, so unset the variable to use stdio."
            )
        if self.max_turns_default > self.max_turns_hard_cap:
            raise ConfigError(
                f"DELEGATE_MAX_TURNS_DEFAULT ({self.max_turns_default}) exceeds "
                f"DELEGATE_MAX_TURNS_HARD_CAP ({self.max_turns_hard_cap})."
            )
        if self.max_total_prefetch_tokens < self.max_file_tokens:
            raise ConfigError(
                f"DELEGATE_MAX_TOTAL_PREFETCH_TOKENS ({self.max_total_prefetch_tokens}) "
                f"is below DELEGATE_MAX_FILE_TOKENS ({self.max_file_tokens}), so no file "
                "could ever be prefetched."
            )
        for name in (
            "max_bash_output_chars",
            "secret_shadow_max_entries",
            "secret_shadow_max_depth",
            "turn_timeout",
            "connect_timeout",
            "dispatch_timeout",
            "run_bash_timeout",
            "status_probe_timeout",
            "keepalive_interval",
            "admission_wait_timeout",
            "max_inflight_seqs",
            "kv_token_budget",
            "large_prefill_tokens",
            "max_inflight_large_prefills",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"DELEGATE_{name.upper()} must be positive.")
        self._check_deadlines_nest()
        for name in ("tool_call_temperature", "one_shot_temperature"):
            if not 0.0 <= getattr(self, name) <= 2.0:
                raise ConfigError(
                    f"DELEGATE_{name.upper()} must be between 0.0 and 2.0; the canonical "
                    "request refuses anything else."
                )
        if self.keepalive_interval * 2 > CLIENT_STDIO_IDLE_TIMEOUT:
            # Refused at startup rather than warned about, because the symptom it causes
            # is invisible from here: the caller is told the call failed, the server keeps
            # working, and the slot stays held until the work ends on its own. Half the
            # timeout rather than all of it, so a beat lands twice inside every window and
            # a late one is still early.
            raise ConfigError(
                f"DELEGATE_KEEPALIVE_INTERVAL ({self.keepalive_interval}) leaves no margin "
                f"under the client's {CLIENT_STDIO_IDLE_TIMEOUT}s stdio idle timeout. A "
                "one-shot sends nothing else, so at this interval the caller abandons the "
                "call while the server works on, holding its admission slot. Use at most "
                f"{CLIENT_STDIO_IDLE_TIMEOUT // 2}."
            )
        self._validate_retry()
        self._validate_overflow()

    def _validate_retry(self) -> None:
        """The retry settings, checked here rather than inline above.

        Split out because `__post_init__` was one branch over the lint threshold, and of
        the two ways past that this is the honest one: these three checks are a single
        concern with a name, so moving them reads better than suppressing a count. The
        remaining checks stay where they are -- scattering the rest to chase a number
        would spread one policy across several functions for no reader's benefit.
        """
        if self.retry_max_attempts < 1:
            raise ConfigError(
                f"DELEGATE_RETRY_MAX_ATTEMPTS ({self.retry_max_attempts}) must be at "
                "least 1. It counts attempts, not retries, so 1 means send once and do "
                "not retry; 0 would mean never send at all."
            )
        for name in ("retry_base_delay", "retry_max_delay"):
            if getattr(self, name) < 0:
                raise ConfigError(f"DELEGATE_{name.upper()} must not be negative.")
        if self.retry_base_delay > self.retry_max_delay:
            raise ConfigError(
                f"DELEGATE_RETRY_BASE_DELAY ({self.retry_base_delay}) exceeds "
                f"DELEGATE_RETRY_MAX_DELAY ({self.retry_max_delay}): the first backoff "
                "would already be above the cap, so the cap would be the only delay ever "
                "used and the base would silently mean nothing."
            )

    def _validate_overflow(self) -> None:
        """The context-overflow settings, checked together because they constrain each other.

        Split out for the reason `_validate_retry` was: one concern with a name reads
        better than four more branches in `__post_init__`. Checked even when the feature is
        disabled -- a setting that only fails once someone arms it fails at the worst
        possible moment, and this file's whole convention is to refuse at load.
        """
        if not 0.0 < self.overflow_reserve_fraction < OVERFLOW_ABORT_AT:
            raise ConfigError(
                f"DELEGATE_OVERFLOW_RESERVE_FRACTION ({self.overflow_reserve_fraction}) "
                f"must be above 0 and below {OVERFLOW_ABORT_AT}. It is a fraction of the "
                "context window, not a token count. At or above the abort threshold the "
                "reserve alone accounts for the whole budget, so every delegation would "
                "abort on its first turn against an empty history -- which is exactly the "
                "shape of the flat-reserve bug this setting is a fraction to avoid."
            )
        for name in ("overflow_plateau_slop_tokens", "overflow_min_growth_tokens"):
            if getattr(self, name) < 0:
                raise ConfigError(f"DELEGATE_{name.upper()} must not be negative.")
        if self.overflow_tightened_keep_tool_results > self.keep_tool_results:
            raise ConfigError(
                f"DELEGATE_OVERFLOW_TIGHTENED_KEEP_TOOL_RESULTS "
                f"({self.overflow_tightened_keep_tool_results}) exceeds "
                f"DELEGATE_KEEP_TOOL_RESULTS ({self.keep_tool_results}): crossing the "
                "first threshold would RELAX retention rather than tighten it, and the "
                "history would grow faster the closer it got to the window."
            )
        if self.overflow_probe_cache_ttl <= 0:
            raise ConfigError(
                "DELEGATE_OVERFLOW_PROBE_CACHE_TTL must be positive. Zero would re-probe "
                "the endpoint on every delegation; there is no value meaning 'never "
                "expire', because a cache that never expires is the bug this setting is "
                "here to prevent."
            )
        if self.overflow_tightened_keep_tool_results < 0:
            raise ConfigError(
                "DELEGATE_OVERFLOW_TIGHTENED_KEEP_TOOL_RESULTS must not be negative."
            )

    # ---- derived helpers ---------------------------------------------------------
    @property
    def effective_workdir_roots(self) -> tuple[str, ...]:
        return self.workdir_roots or self.workspace_roots

    def estimate_tokens(self, nbytes: int, ext: str = "") -> int:
        """Estimate tokens for a payload, biased to over-count. ADR-0019.

        Uses the measured per-extension ratio; an unknown extension falls back to the
        worst case observed, because guessing high costs a little idle capacity while
        guessing low queues a request until it times out.
        """
        ratio = BYTES_PER_TOKEN.get(ext.lower(), BYTES_PER_TOKEN_DEFAULT)
        return int(nbytes / ratio)


def env_name(field_name: str) -> str:
    return "DELEGATE_" + field_name.upper()


def _coerce(raw: str, current: Any, field_name: str) -> Any:
    if isinstance(current, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"{env_name(field_name)}={raw!r} is not a boolean.")
    if isinstance(current, tuple):
        return tuple(p for p in (s.strip() for s in raw.split(os.pathsep)) if p)
    if isinstance(current, int):
        try:
            return int(raw.strip())
        except ValueError as e:
            raise ConfigError(f"{env_name(field_name)}={raw!r} is not an integer.") from e
    if isinstance(current, float):
        try:
            return float(raw.strip())
        except ValueError as e:
            raise ConfigError(f"{env_name(field_name)}={raw!r} is not a number.") from e
    return raw


ENV_FILE_VAR = "DELEGATE_ENV_FILE"

# config.py lives at <root>/src/claude_delegate_local/config.py.
REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines. Blank lines and `#` comments are skipped.

    Deliberately not dotenv, and the differences matter more than the similarities: no
    variable interpolation, no multi-line values, and **no escape processing at all**. A
    value is taken literally, because `DELEGATE_WORKSPACE_ROOTS` on Windows is a path full
    of backslashes and unescaping it would corrupt it silently.

    One pair of matching surrounding quotes is stripped: someone who quotes a value
    containing spaces means the spaces, not the quotes.

    A leading `export ` is stripped rather than rejected. Not for shell compatibility --
    without it the line parses to a key named `export FOO`, which no setting matches, so
    the value is dropped while the file still looks like it was read. A name that cannot be
    an environment variable raises instead, for the same reason: a typo that silently
    changes nothing is the worst of the three outcomes.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key and not key.replace("_", "").isalnum():
            raise ConfigError(
                f".env line {line!r} has {key!r} on the left of the '=', which cannot be "
                f"an environment variable name. Nothing would have read it."
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _env_file_values(explicit: str | Path | None) -> dict[str, str]:
    """Values from a .env file, or an empty mapping if there is none to read.

    An explicitly requested file that does not exist is an error rather than an empty
    result. Asking for a specific file and silently getting the defaults is the failure
    mode this project keeps finding: no error, no symptom, wrong behaviour.
    A discovered `<repo>/.env` is optional, because not having one is normal.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(
                f"{ENV_FILE_VAR} names {path}, which does not exist. Point it at a real "
                f"file or unset it; it is not ignored when wrong."
            )
        return parse_env_file(path.read_text(encoding="utf-8"))
    found = REPO_ROOT / ".env"
    return parse_env_file(found.read_text(encoding="utf-8")) if found.is_file() else {}


def load(environ: dict[str, str] | None = None,
         env_file: str | Path | None = None) -> Config:
    """Build a Config from the environment, validating as we go.

    Tuple-valued settings split on os.pathsep, so they read naturally on either host:
    semicolons on Windows, colons elsewhere.

    A `.env` file beside the repository root is read as a **fallback**: a value already
    present in the real environment wins, so an explicit override still works. The file is
    read at all because the README has always told you to create one, and until now nothing
    did -- the launch path is `wsl.exe -e claude-delegate-local-mcp`, and an `env` key in an
    MCP client's config sets variables for `wsl.exe` on the Windows side, one hop short of
    the Linux process that reads them. Crossing that boundary needs WSLENV as well, which
    fails silently when forgotten. (ADR-0027)

    Passing `environ` explicitly suppresses discovery, so a test never picks up whatever
    .env happens to sit in the working tree. `env_file` overrides both.
    """
    discovered: dict[str, str] = {}
    if env_file is not None:
        discovered = _env_file_values(env_file)
    elif environ is None:
        discovered = _env_file_values(os.environ.get(ENV_FILE_VAR))

    live = os.environ if environ is None else environ
    src: dict[str, str] = {**discovered, **{k: v for k, v in live.items() if v != ""}}
    defaults = Config.__dataclass_fields__
    kwargs: dict[str, Any] = {}
    for f in fields(Config):
        raw = src.get(env_name(f.name))
        if raw is None or raw == "":
            continue
        sentinel = defaults[f.name].default
        kwargs[f.name] = _coerce(raw, sentinel, f.name)
    # ext_allowlist is compared case-insensitively and needs leading dots.
    if "ext_allowlist" in kwargs:
        kwargs["ext_allowlist"] = tuple(
            e.lower() if e.startswith(".") else "." + e.lower()
            for e in kwargs["ext_allowlist"]
        )
    return Config(**kwargs)


def describe() -> list[dict[str, Any]]:
    """The rows `scripts/gen_config_docs.py` renders. Introspection, not a second list."""
    out = []
    for f in fields(Config):
        default = f.default
        out.append(
            {
                "env": env_name(f.name),
                "field": f.name,
                "type": type(default).__name__,
                # Rendered with a FIXED separator, never os.pathsep. The doc generator
                # runs on Windows locally and Linux in CI, and os.pathsep differs between
                # them -- so embedding it here made the generated file impossible to match
                # across platforms, and the freshness check unsatisfiable. The real
                # separator is documented once in the generated header instead.
                "default": ", ".join(default) if isinstance(default, tuple) else default,
                "unit": f.metadata.get("unit", ""),
                "description": " ".join(f.metadata.get("description", "").split()),
                "required": f.name == "workspace_roots",
            }
        )
    return out
