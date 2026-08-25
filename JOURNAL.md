<!-- BUDGET: 300 -->
# Journal

Things that took real work to figure out, so the next person does not pay for them
twice. Append-only, newest last. Not a diary: routine work leaves no entry.

The bar is roughly "this cost more than an hour, or it was surprising enough that I
would have got it wrong again in six months".

---

## 2026-08-25 — A generated doc can be validated against code that no longer exists

Found while testing the docs gate, by accident, and it would have quietly undermined the
whole anti-drift mechanism.

The generator imports `config.py` and compares its output to the committed markdown.
Changing a default from `6` to `9` and back again inside one second produced a
`__pycache__` entry that Python considered valid — because it validates a `.pyc` on
**(mtime, size)**, and `6` and `9` are the same byte length. The generator then read the
cached compile and reported the document as current against a version of the code that
had already been replaced.

The trap within the trap: `python -B` does **not** fix it. That flag stops Python
*writing* bytecode; it happily *reads* an existing stale cache. I "fixed" it that way
first and the test still returned the wrong value, which is the only reason I noticed.

What works is redirecting the cache somewhere empty for the run, so a fresh compile is
forced:

    sys.pycache_prefix = tempfile.mkdtemp(prefix="cdl-gen-pyc-")

Applies to every generator, not just this one. Any tool whose entire job is comparing a
committed artefact against live source must not trust a cached compile of that source.

## 2026-08-25 — The serving stack's documentation and its running behaviour disagree

Three instances found in one afternoon, so treat this as the norm rather than bad luck.
Probe the endpoint; do not trust the repository.

- `thinking_token_budget` is documented as gated by a GPU-hotfix boot flag. The server's
  actual 400 response names a completely different switch: `VLLM_USE_V2_MODEL_RUNNER=0`.
- The example environment file ships maximum reasoning effort, while two of the stack's
  own documents still recommend the low setting. A changelog entry shows the default was
  raised and those documents were never updated.
- Published prefill figures are pessimistic against measurement: 32K tokens is
  documented at 23-61s, measured at 17s. Probably measured under concurrency, but the
  gap matters when sizing a budget against it.

Worth noting this is the same failure class this project's own documentation strategy is
built to prevent, observed in the wild, in the stack we depend on.

## 2026-08-25 — Rationing context by bytes is the wrong unit, and the error is not small

The prefetch budget started in bytes because that is what a filesystem hands you. Bytes
per token turn out to vary by more than 2x across the file types a delegation actually
sees: 1.78 for punctuation-heavy JSON, 2.08 for a TOML lockfile, 3.89 for Python source,
4.16 for Python with dense docstrings.

Two consequences, both missed until measured. A `bytes / 3` estimator that was described
as conservative under-counts JSON by 41%. And a per-file cap in bytes silently allows
twice as much *context* for a data file as for a source file, which is the opposite of
what anyone wants — data files are the ones worth truncating.

Denominating in estimated tokens, with per-extension ratios rounded down from
measurement, fixes both. See ADR-0019.

The general lesson: when rationing a resource, denominate the budget in the unit the
resource is actually consumed in, even when a cheaper proxy is right there.
