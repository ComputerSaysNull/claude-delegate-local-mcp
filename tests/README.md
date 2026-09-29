<!-- BUDGET: 80 -->
# Tests

How a test is written here, and the traps that have already cost someone a day. Setting up
and running the suite is [CONTRIBUTING](../CONTRIBUTING.md#setup)'s; the rules a machine
cannot check, red before green among them, are [CLAUDE.md](../CLAUDE.md)'s.

## Rules

Regression tests are named after the bug and live in `tests/regression/`. If you fix
something subtle, the test goes in the same commit.

**Negative-test every check**: assert it fires on a real violation, not merely that it
passes on clean input. CLAUDE.md lists the shapes of check already found unable to fail.

Mark anything needing the live cluster or a real `bwrap` as `@pytest.mark.integration`.

**Neither kind runs in CI, and the `bwrap` kind cannot**: on a runner it builds its user
namespace, then fails to configure loopback for want of `CAP_NET_ADMIN` (JOURNAL
2026-08-29). A green CI run is therefore no evidence about the sandbox. Change `sandbox.py`
and you must run the suite in WSL yourself before pushing.

**A test whose premise is "this checkout looks like mine" passes locally and fails in CI** —
a pinned baseline, a moving `main`, a host where `bwrap` installs but cannot unshare.
`conftest.py` holds `BASELINE_COMMIT` and `baseline_blob` for the first two.

## Proving the red

- **When a fix changes a signature, the red is a measurement, not a failing test.** The new
  test cannot import against the old code, and an `ImportError` proves nothing. Load the
  committed module by path, drive the old function with the old shapes, and quote its numbers.
- **To prove a red after the fix is written**, load the committed file by path —
  `git show main:path > tmp.py`, then `importlib.util.spec_from_file_location` — and assert
  each new behaviour is absent from it. The working tree is untouched: no stash, no revert.
- **Reverting a mutation with `git checkout -- <file>` reverts the fix too.** Mutate with an
  edit you undo by the same edit, or on a copy loaded by path.

## Doubles, clocks and patches

- **`monkeypatch` one layer below the behaviour you pin**, or the patch steps over the
  `except` under test and the test fails against correct code. For a vanished file, patch
  `open_resolved`.
- **A deadline cannot be driven through the whole MCP stack**: `run_delegation` has no clock
  seam, so a handler that hangs hangs for ever. Test deadlines at `complete_with_retry` with
  a `FakeClock` and the `tick_sleep` seam, as every existing deadline test does.
- **A fake clock and real time diverge inside a watchdog.** The watchdog waits on the wall,
  so a double that advances a fake clock finishes before the first real tick and the
  deadline never fires. `tick_sleep` is the seam that fixes it.
- **`httpx.MockTransport` buffers a plain `Response`**, so `.text` works whether or not the
  code read it. Only a lazy body — `content=<async generator>` — can fail a test that a
  refusal carried its body.
- **A fixture holding a PEM header is written by concatenating fragments**, never as a
  literal: the sandbox's content scan would shadow the test file itself during any
  `run_bash` that binds the repository.
- **`e=$?` before reading `PIPESTATUS` clobbers it**, and so do `eval` and redirecting the
  whole pipeline: every shape then reports `[0]`. Capture `PIPESTATUS` first.

## Running, and testing the gate

- **`-p no:xdist` is a parse error**: `addopts` carries `-n` and `--maxprocesses`. Run
  serially with `-n 0`.
- **`--durations` from a parallel run is inflated, sometimes tenfold.** Trust a duration
  only from `-n 0`, or where a band of tests repeats one figure exactly — a constant being
  waited out, not contention.
- **Run the gate with changes staged.** It diffs against HEAD, so with nothing staged it
  prints `0 file(s) changed` and passes having checked no ownership at all.
- **Reproduce CI's gate invocation, not the hook's**: `--mode ci --diff origin/main...HEAD`.
  `--mode pre-commit` diffs against HEAD, so once a branch has a commit it sees only what
  changed since, and can block on a document the commit already updated.
- **The doc-reference check is blind to a dotted link target on Windows**, which strips
  trailing dots, so a link written as an illustration passes locally and fails CI. Write
  such an illustration without its parentheses.
- **Do not test the generated-doc check with a scratch comment.** A source edit that changes
  no rendered output leaves the document fresh, so the gate passes and looks broken. Change
  what the generator renders instead.
