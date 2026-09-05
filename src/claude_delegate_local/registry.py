"""The model registry: one explicit row per model, replacing prefix matching.

Why a registry at all (ADR-0009): the serving stack runs one model per inference
instance. A second model means a second container on a second port, with its own base
URL. Matching a model *name* against a prefix table cannot express that -- the URL is
not derivable from the name.

The ancestor project kept five prefix-keyed tables that had to stay mutually consistent
by hand, resolved longest-prefix-wins:

    _OPENAI_FORMAT_PREFIXES   -> api_format
    HIGH_REASONING_PREFIXES   -> default_effort
    PROVIDER_MAX_TOKENS_CAP   -> max_tokens_cap
    MODEL_BUDGET_POLICY       -> default_max_tokens
    PROVIDER_CONCURRENCY      -> concurrency

All five collapse into the fields below. That is strictly less state, and it removes an
entire bug class its own comments record -- one provider deliberately excluded from the
format table to stop it breaking reasoning, another silently getting the wrong budget
because its name lacked an expected suffix.

Not a port. This file is new.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import EFFORT_LEVELS, Config, ConfigError

API_FORMATS = ("openai", "anthropic")

# Only the OpenAI adapter ships (ADR-0008). "anthropic" is accepted as a *declared*
# value so a registry file written today stays valid when the adapter lands, but
# resolving one raises rather than silently doing something else.
IMPLEMENTED_FORMATS = ("openai",)

_REQUIRED = ("base_url", "served_model_id")

# One definition, referenced twice below. It was written out at both the dataclass
# default and the parser's fallback, which is the duplicated-literal shape this project
# derives BYTES_PER_TOKEN_DEFAULT to avoid: two copies of a default drift, and the one
# that drifts is whichever the reader did not check.
DEFAULT_CONTEXT_WINDOW = 131072


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One servable model. Everything needed to talk to it, in one place."""

    key: str
    base_url: str
    served_model_id: str
    api_format: str = "openai"
    api_key_env: str = ""
    context_window: int = DEFAULT_CONTEXT_WINDOW
    default_effort: str = ""          # "" means fall back to Config.thinking_default
    max_tokens_cap: int = 0           # 0 means no per-model cap
    concurrency: int = 5              # this endpoint's own limit, checked alongside the global one
    # True when models.toml said nothing and the default above was assumed. Recorded
    # because the two cases need different advice: a window the operator set and got
    # wrong is corrected, while one they never set was never a claim at all. Without
    # this, the mismatch report told an operator their file gave a number it does not
    # mention anywhere.
    context_window_defaulted: bool = False
    is_default: bool = False

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"

    @property
    def models_url(self) -> str:
        # Bare vLLM exposes /v1/models but NOT the /health/liveliness the ancestor
        # probed -- that endpoint belongs to a proxy it used to sit behind. Probing the
        # wrong path made a healthy server look unreachable.
        return f"{self.base_url.rstrip('/')}/v1/models"

    @property
    def metrics_url(self) -> str:
        # Not under /v1: the serving stack publishes Prometheus text at the root, which
        # is where the metrics were found on 2026-09-05. An endpoint without it answers
        # 404 and is reported as having nothing to say rather than as unhealthy.
        return f"{self.base_url.rstrip('/')}/metrics"

    def effective_effort(self, cfg: Config) -> str:
        return self.default_effort or cfg.thinking_default

    def cap_tokens(self, requested: int) -> int:
        return min(requested, self.max_tokens_cap) if self.max_tokens_cap else requested


class RegistryError(ConfigError):
    """A malformed registry. Raised at startup, never at first use."""


def _validate(key: str, raw: dict, path: Path) -> ModelEntry:
    where = f"{path}: [models.{key}]"

    unknown = set(raw) - set(ModelEntry.__dataclass_fields__) - {"default"}
    if unknown:
        raise RegistryError(
            f"{where} has unknown field(s) {sorted(unknown)}. Refusing rather than "
            "ignoring them: a typo in a registry key would otherwise cost you the "
            "setting silently."
        )
    for field_name in _REQUIRED:
        if not raw.get(field_name):
            raise RegistryError(f"{where} is missing required field {field_name!r}.")

    api_format = raw.get("api_format", "openai")
    if api_format not in API_FORMATS:
        raise RegistryError(
            f"{where} api_format={api_format!r} is not one of {API_FORMATS}."
        )
    if api_format not in IMPLEMENTED_FORMATS:
        raise RegistryError(
            f"{where} api_format={api_format!r} is a recognised format but no adapter "
            f"for it ships yet (only {IMPLEMENTED_FORMATS} is implemented). See "
            "ADR-0008: the canonical message shape and the Backend seam are kept so "
            "adding one is additive, but it has not been added."
        )

    effort = raw.get("default_effort", "")
    if effort and effort not in EFFORT_LEVELS:
        raise RegistryError(
            f"{where} default_effort={effort!r} is not one of {EFFORT_LEVELS}. Refused "
            "at load: these are this project's levels, which the backend adapter "
            "translates to the server's own vocabulary. An unlisted one has no "
            "translation."
        )

    base_url = str(raw["base_url"]).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise RegistryError(f"{where} base_url must start with http:// or https://.")
    if base_url.endswith("/v1"):
        raise RegistryError(
            f"{where} base_url should not include the /v1 suffix -- the server appends "
            f"the API path itself. Use {base_url[:-3]!r}."
        )

    for num in ("context_window", "max_tokens_cap", "concurrency"):
        if num in raw and (not isinstance(raw[num], int) or raw[num] < 0):
            raise RegistryError(f"{where} {num} must be a non-negative integer.")
    if raw.get("concurrency", 5) == 0:
        raise RegistryError(f"{where} concurrency must be at least 1.")

    return ModelEntry(
        key=key,
        base_url=base_url,
        served_model_id=str(raw["served_model_id"]),
        api_format=api_format,
        api_key_env=str(raw.get("api_key_env", "")),
        context_window=int(raw.get("context_window", DEFAULT_CONTEXT_WINDOW)),
        context_window_defaulted="context_window" not in raw,
        default_effort=effort,
        max_tokens_cap=int(raw.get("max_tokens_cap", 0)),
        concurrency=int(raw.get("concurrency", 5)),
        is_default=bool(raw.get("default", False)),
    )


@dataclass(frozen=True, slots=True)
class Registry:
    entries: dict[str, ModelEntry]
    default_key: str

    def resolve(self, name: str | None) -> ModelEntry:
        """Look up a model, or the default. Never guesses from the name."""
        if not name:
            return self.entries[self.default_key]
        if name in self.entries:
            return self.entries[name]
        # A served_model_id is what a caller sees in backend_status(), so accept it too
        # rather than making them learn two names for one thing.
        for entry in self.entries.values():
            if entry.served_model_id == name:
                return entry
        raise RegistryError(
            f"Unknown model {name!r}. Registered: {sorted(self.entries)}. Models are "
            "declared explicitly in the registry file -- there is no name-pattern "
            "fallback, by design (ADR-0009)."
        )

    def __len__(self) -> int:
        return len(self.entries)


def load(cfg: Config) -> Registry:
    path = Path(cfg.models_file).expanduser()
    if not path.exists():
        raise RegistryError(
            f"Model registry not found at {path}. Copy models.toml.example to "
            f"{path.name} and fill in your endpoint. It is gitignored on purpose: it "
            "names a host."
        )
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(f"{path} is not valid TOML: {e}") from e

    raw_models = doc.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise RegistryError(
            f"{path} declares no models. Expected at least one [models.<key>] table."
        )

    entries = {key: _validate(key, raw, path) for key, raw in raw_models.items()}

    if cfg.default_model:
        if cfg.default_model not in entries:
            raise RegistryError(
                f"DELEGATE_DEFAULT_MODEL={cfg.default_model!r} is not in {path}. "
                f"Registered: {sorted(entries)}."
            )
        return Registry(entries=entries, default_key=cfg.default_model)

    flagged = [k for k, e in entries.items() if e.is_default]
    if len(flagged) == 1:
        return Registry(entries=entries, default_key=flagged[0])
    if not flagged and len(entries) == 1:
        return Registry(entries=entries, default_key=next(iter(entries)))
    if not flagged:
        raise RegistryError(
            f"{path} has {len(entries)} models and none marked `default = true`. Mark "
            "one, or set DELEGATE_DEFAULT_MODEL. Refusing to pick for you: silently "
            "choosing a model changes cost and behaviour."
        )
    raise RegistryError(
        f"{path} marks {len(flagged)} models as default ({sorted(flagged)}). Exactly one."
    )
