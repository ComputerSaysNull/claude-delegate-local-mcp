"""The default extension allowlist refused common source and config types.

Layer 2 fails closed, which is its point (ADR-0006): an unknown type is refused rather than
trusted, so binaries, archives and keystores never reach a model whatever they are named.
But the default had grown by accretion and was inconsistent with itself -- `.mjs` without
`.cjs`, `.sh` without `.ps1`, no XML at all -- and whole families of plain-text source were
missing: .NET and MSBuild, the JVM languages, shells, web frameworks, build and
infrastructure files. Each cost a skipped prefetch or a refused read, and a turn.

The allowlist decides what the file tools may read and write, never what runs: `run_bash`
does not consult it, and executes only inside the sandbox.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import paths
from claude_delegate_local.config import Config


def _cfg() -> Config:
    return Config(workspace_roots=(".",))  # type: ignore[arg-type]


# One or two from each family added, by the name a real file would have.
@pytest.mark.parametrize("name", [
    "eslint.config.cjs", "src/App.vue", "styles/site.scss",
    "scripts/build.ps1", "scripts/run.bash",
    "src/App.csproj", "src/Program.fs", "src/Page.xaml", "Directory.Build.props",
    "build.gradle", "src/Main.scala",
    "lib/main.dart", "lib/app.ex",
    "config/manifest.xml", "api/schema.graphql", "proto/service.proto", "infra/main.tf",
    "docs/guide.adoc",
    ".editorconfig", ".gitattributes", "Justfile", "LICENSE",
])
def test_the_default_allows_it(name):
    assert paths.extension_refusal(_cfg(), name) is None


@pytest.mark.parametrize("name", [
    "release.zip", "logo.png", "data.sqlite", "cert.p12", "vault.kdbx",
    # Left out deliberately: each commonly carries credentials.
    "app.properties", "prod.tfvars", ".npmrc", ".netrc",
])
def test_what_it_should_refuse_is_still_refused(name):
    """The control: the list grew, it did not stop being a list."""
    assert paths.extension_refusal(_cfg(), name) is not None
