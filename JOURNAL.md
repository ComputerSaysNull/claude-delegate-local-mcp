<!-- BUDGET-PER-ENTRY: 55 -->
<!-- ARCHIVE-AT: 600 -->
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

## 2026-08-25 — bwrap in WSL2: two traps, and an error message that lies

Provisioning Ubuntu 24.04 under WSL2 and standing up the sandbox turned up two failures
that would each have cost an afternoon at M5.

**The lib64 symlink is mandatory, not optional, and the error blames the wrong thing.**
An empty-root sandbox with `--ro-bind /usr /usr` and `--symlink usr/bin /bin` fails with:

    bwrap: execvp /usr/bin/echo: No such file or directory

The binary is present and readable. What is missing is `/lib64/ld-linux-x86-64.so.2`, the
ELF interpreter — and when the kernel cannot find a binary's interpreter it returns ENOENT,
which surfaces as "No such file or directory" against the *executable*. Every diagnostic
instinct points at the wrong file.

So `--symlink usr/lib64 /lib64` is required on x86-64. The planning notes had it as
"if present on the distro", which is wrong: without it nothing dynamically linked runs at
all. Add `--symlink usr/sbin /sbin` for the same reason.

**DNS inside the sandbox needs a WSL-specific bind.** With the network deliberately
re-shared for an agent that opted in, connections by IP succeeded and connections by
hostname failed. The cause: under WSL, `/etc/resolv.conf` is a symlink to
`/mnt/wsl/resolv.conf`. Binding `/etc` therefore binds a *dangling* symlink, and name
resolution fails while connectivity is fine. The fix is to bind `readlink -f
/etc/resolv.conf` at its own real path as well.

**A test worth keeping: verify network denial by IP, not by hostname.** With resolv.conf
unbound, a hostname request fails whether or not the network namespace is actually
isolated. Testing by hostname alone would have reported the sandbox as network-tight when
it might merely have had broken DNS. Only a by-IP attempt distinguishes the two, and only
the by-IP result is evidence.

Verified working after the fixes: commands run, python3 resolves, network denied by
default (000 by IP), the real HOME absent rather than read-only, `~/.ssh` absent, a bound
workdir writable, and network available when explicitly re-shared.

## 2026-08-25 — The 9p bridge, measured: the penalty is real but it is not uniform

The plan flagged `/mnt/c` performance as an open risk. Measured, same tree, same machine,
warm caches, WSL2 Ubuntu 24.04:

    operation                       /mnt/c (9p)      ext4     penalty
    git status                            0.43s     0.00s      ~100x
    read every tracked file               0.16s     0.02s         8x
    write 300 small files                 1.96s     0.01s      ~200x
    create a venv                        68.92s     2.51s        27x
    pytest, full suite (avg of 3)          7.11s     0.59s        12x
    pytest, one file                       0.56s     0.20s        2.8x

The shape matters more than the headline. Ratios look alarming; absolute numbers are
mostly survivable. A 12x penalty on the full suite is 7 seconds, not 70, and the
self-verification loop still costs zero Claude tokens. Metadata-heavy work is where 9p
genuinely hurts: venv creation at 69 seconds is the one number that stings, and it is
rare.

Also learned, and it constrains the obvious workaround: **Windows cannot use a `\wsl$\`
UNC path as a process working directory.** `CreateProcess` refuses it outright. So
"keep Claude Code on Windows but move the repo onto ext4" does not work as stated —
Windows-side tools that need a cwd, including any shell command Claude runs, cannot
operate there without mapping a drive letter first.
