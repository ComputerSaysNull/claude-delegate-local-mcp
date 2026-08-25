# security/

Machine-checkable safety lists. Deliberately small and deliberately shared.

| File | Consumed by | Purpose |
| --- | --- | --- |
| `secret_globs.txt` | `paths.py` (layer 3) **and** `scripts/docs_gate.py` | Paths a delegated model must never receive, and git must never accept |
| `allowed_emails.txt` | `scripts/docs_gate.py` | The only addresses permitted as a commit author/committer or inside a tracked file |
| `forbidden_strings.txt` | `scripts/docs_gate.py` | **Untracked, local only.** Host identifiers -- see below |

## Why `forbidden_strings.txt` is not committed

It holds the literal identifiers of the serving cluster: short hostname, fully
qualified tailnet name, and address. A committed file listing the strings you do
not want committed is itself the leak it is trying to prevent.

It lives here untracked (`.gitignore`d), and CI reads the same content from a
repository secret. The committed backstop is a set of *patterns* in the gate --
private-IP ranges, `host:port` shapes, and short-hostname forms -- which catch
the class without naming the instance.
