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
TRANSPORTS = ("stdio", "streamable-http")

# Bytes per token, MEASURED against this model's own tokenizer (ADR-0019). These vary by
# more than 2x across file types, which is why the prefetch budget is denominated in
# tokens rather than bytes -- 128 KiB is 33K tokens of Python but 72K tokens of JSON.
#
# The measurements themselves live in JOURNAL 2026-08-25 and are not repeated here; the
# values below are those numbers rounded down, which is a different thing.
#
# Each entry is rounded DOWN from the measurement, so an estimate errs toward
# over-counting: over-counting wastes a little admission capacity, under-counting queues
# a request until it times out. The unknown-extension default is the worst case observed.
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
        40000,
        "Per-file prefetch cap, in ESTIMATED TOKENS rather than bytes. Bytes were the "
        "wrong unit: the same byte limit is worth 2x more tokens for JSON than for "
        "Python. 40K tokens is roughly 155 KiB of source or 68 KiB of JSON, and costs "
        "about 20s of prefill. A file over the cap is skipped whole, never truncated -- "
        "source cut mid-function is worse than absent, because the model will "
        "confidently repair code it never saw.",
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
    max_read_chars: int = _f(
        50000,
        "Cap on one read_file response. The model is told the true total and how to page, "
        "so it continues by range rather than re-reading.",
        unit="chars",
    )
    max_write_bytes: int = _f(8388608, "Cap on one write_file call.", unit="bytes")
    run_bash_timeout: int = _f(120, "Per-command timeout for run_bash.", unit="seconds")

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
        40,
        "Ceiling no agent file or caller may exceed. Stops an agent definition asking for "
        "500 turns and occupying the cluster for hours.",
    )
    keep_tool_results: int = _f(
        6,
        "Most recent tool results kept intact; older ones collapse to a one-line stub. "
        "Every turn resends the whole history, so this is what stops quadratic growth.",
    )
    max_batch_size: int = _f(12, "Largest accepted delegate_batch request.")

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
        3600,
        "Whole-delegation timeout, spanning every retry and every empty-answer recovery "
        "stage. Claude Code's own wall-clock MCP timeout defaults to about 28 hours so it "
        "is not the binding limit; its stdio IDLE timeout of 30 minutes is lower than this "
        "default, and the per-turn progress notification that answers that is ADR-0018, "
        "arriving with the turn loop. This bounds the wait, not the client's patience.",
        unit="seconds",
    )
    retry_max_attempts: int = _f(3, "Attempts on a retryable backend status.")
    retry_base_delay: float = _f(1.0, "Exponential backoff base.", unit="seconds")
    retry_max_delay: float = _f(
        20.0,
        "Cap on a single wait between attempts, including one the endpoint asked for via "
        "Retry-After. Uncapped, a large or hostile Retry-After stalls a call far past "
        "anything turn_timeout was meant to bound, and the wait happens between requests "
        "where no HTTP timeout applies to it. Kept well under the 30-minute stdio idle "
        "timeout because nothing yet emits a progress notification to hold that off -- "
        "that is ADR-0018 and lands with the turn loop.",
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

    # ---- sandbox (ADR-0010) ------------------------------------------------------
    sandbox_enabled: bool = _f(
        True,
        "Confine run_bash with bubblewrap. When enabled and bwrap is absent, run_bash "
        "REFUSES rather than running unconfined. Setting this to 0 is an explicit, "
        "logged choice to run shell commands with no confinement.",
    )
    bwrap_bin: str = _f("bwrap", "bubblewrap binary name or path.")
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

    # ---- agents ------------------------------------------------------------------
    agents_dir: str = _f(
        "~/.claude/agents",
        "Third and last place an agent definition is looked for, after the workspace's "
        "own agents and skills directories.",
    )

    # ---- transport ---------------------------------------------------------------
    transport: str = _f(
        "stdio",
        f"One of {TRANSPORTS}. Adding the HTTP transport is a real integration task, not "
        "a flag flip: session handling and content serialisation differ.",
    )
    http_port: int = _f(8765, "Port, used only by the HTTP transport.")

    # ---------------------------------------------------------------------------
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
            raise ConfigError(
                f"DELEGATE_TRANSPORT={self.transport!r} is not one of {TRANSPORTS}."
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
            "turn_timeout",
            "connect_timeout",
            "dispatch_timeout",
            "run_bash_timeout",
            "status_probe_timeout",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"DELEGATE_{name.upper()} must be positive.")
        if self.connect_timeout > self.turn_timeout:
            raise ConfigError(
                f"DELEGATE_CONNECT_TIMEOUT ({self.connect_timeout}) exceeds "
                f"DELEGATE_TURN_TIMEOUT ({self.turn_timeout}): the connect phase cannot "
                "be allowed to outlast the whole call it is part of."
            )
        for name in ("tool_call_temperature", "one_shot_temperature"):
            if not 0.0 <= getattr(self, name) <= 2.0:
                raise ConfigError(
                    f"DELEGATE_{name.upper()} must be between 0.0 and 2.0; the canonical "
                    "request refuses anything else."
                )
        if self.turn_timeout > self.dispatch_timeout:
            raise ConfigError(
                f"DELEGATE_TURN_TIMEOUT ({self.turn_timeout}) exceeds "
                f"DELEGATE_DISPATCH_TIMEOUT ({self.dispatch_timeout}): a single turn "
                "could outlive the delegation containing it."
            )
        self._validate_retry()

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
