#!/usr/bin/env python3
"""Record what the endpoint actually returns, dated, locally, never published.

Writes one capture to `local/endpoint-captures/`, which is untracked *and* on the
secret denylist. See ADR-0052 for why that directory is not a generated document
in `docs/`, and read that before moving it there.

Needs the endpoint. Nothing in the gate or the test suite calls this, deliberately:
a check that needs the cluster up is a check that fails when the cluster is down,
which says nothing about the repository. `diff_endpoint_captures.py` is the half
that runs offline.

    python scripts/probe_endpoint.py                 # write today's capture
    python scripts/probe_endpoint.py --print         # and echo it

Three shapes are elicited, because the adapter parses all three and one plain
completion would miss two of them: a normal reply, a reply truncated at the token
cap, and a reply that calls a tool.

**The allowlist is the only control here.** A tracked file would be scanned by the
gate; these never are, so what may be recorded is decided in this file and nowhere
else. Values are recorded except where `VALUE_WITHHELD` says otherwise, and metric
labels are stripped as a rule rather than as a side effect of wanting names -- a
label carries configuration and handler paths, and once LMCache is enabled
`kv_transfer_params` is where a transfer target would appear.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "local" / "endpoint-captures"

# Recorded as presence-and-type only, never by value. The first five echo the
# prompt, which for this server is repository source. `kv_transfer_params` is null
# today and is where a KV transfer target appears once LMCache is enabled -- the
# host-identifier class exactly, and the reason this list is not "things that are
# currently big".
VALUE_WITHHELD = frozenset({
    "prompt_text", "prompt_token_ids", "token_ids", "logprobs", "prompt_logprobs",
    "kv_transfer_params",
})

TOOL = {
    "type": "function",
    "function": {
        "name": "multiply",
        "description": "Multiply two integers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}


def endpoint() -> tuple[str, str, str]:
    """base_url, served_model_id and any bearer token, from the registry.

    The URL is returned so requests can be made and is never written to a capture,
    printed, or included in an error message raised from here.
    """
    path = Path(os.environ.get("DELEGATE_MODELS_FILE") or ROOT / "models.toml")
    if not path.exists():
        sys.exit(f"no registry at {path.name}; copy models.toml.example and fill it in")
    models = tomllib.loads(path.read_text(encoding="utf-8"))["models"]
    key = next(k for k, v in models.items() if v.get("default")) if any(
        v.get("default") for v in models.values()) else next(iter(models))
    entry = models[key]
    var = entry.get("api_key_env") or ""
    return (entry["base_url"].rstrip("/"), entry["served_model_id"],
            os.environ.get(var, "") if var else "")


def post(base: str, path: str, body: dict, key: str) -> tuple[int, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        # The body is deliberately not recorded: a 4xx or 5xx can carry a traceback
        # with cluster paths in it, which is the thing this whole file exists to keep
        # out of a file nothing scans.
        return e.code, {"error": "body withheld by policy"}


def get_text(base: str, path: str, key: str) -> tuple[int, str]:
    req = urllib.request.Request(base + path)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def shape(node: Any, path: str = "") -> dict[str, str]:
    """Field path -> type, carrying values only where the allowlist permits."""
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if k in VALUE_WITHHELD:
                out[p] = f"{type(v).__name__} (value withheld)"
            elif isinstance(v, (dict, list)):
                out.update(shape(v, p))
            else:
                out[p] = f"{type(v).__name__} = {v!r}"
    elif isinstance(node, list):
        if not node:
            out[path] = "list (empty)"
        else:
            out.update(shape(node[0], f"{path}[0]"))
            out[f"{path}[]"] = f"list of {len(node)}"
    return out


def metric_names(text: str) -> list[str]:
    """Names only. Labels are stripped as a rule, not because names were wanted.

    `vllm:cache_config_info` carries the cache configuration in its labels and
    `http_request_*` carry handler paths. A value that is actually needed is read
    live -- a capture answers what the endpoint *offers*, not what it currently says.
    """
    names = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        names.add(line.split("{", 1)[0].split(" ", 1)[0])
    return sorted(names)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--print", action="store_true", dest="echo")
    args = ap.parse_args()

    base, model, key = endpoint()
    capture: dict[str, Any] = {
        "captured": datetime.now(UTC).date().isoformat(),
        "served_model_id": model,
        "note": "Presence and type. Values only where the probe's allowlist permits.",
    }

    probes = {
        "normal": {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 64},
        "truncated": {"messages": [{"role": "user", "content": "Count slowly to fifty."}],
                      "max_tokens": 4},
        "tool_call": {"messages": [{"role": "user", "content": "What is 6 times 7?"}],
                      "max_tokens": 256, "tools": [TOOL], "tool_choice": "auto"},
    }
    for name, extra in probes.items():
        status, payload = post(base, "/v1/chat/completions",
                               {"model": model, "temperature": 0.0, **extra}, key)
        capture[name] = {"status": status, "fields": shape(payload)}

    status, body = get_text(base, "/metrics", key)
    capture["metrics"] = {"status": status, "names": metric_names(body)}

    status, body = get_text(base, "/v1/models", key)
    served = (json.loads(body).get("data") or [{}])[0] if status == 200 and body else {}
    # `max_model_len` is the one value worth keeping: `backend_status` reports the
    # registry's declared context window and never cross-checks it against this.
    capture["models_endpoint"] = {"status": status, "id": served.get("id"),
                                  "max_model_len": served.get("max_model_len")}

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"endpoint-{capture['captured']}-{model}.json"
    path.write_text(json.dumps(capture, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    if args.echo:
        print(json.dumps(capture, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
