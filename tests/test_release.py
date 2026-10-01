"""A release is a tag naming the package's own version, and nothing else may publish one.

The version was `0.0.0` and nothing was published, so a reader in another repository had
nothing to pin the transcript schema to, and a colleague's `--doctor` nothing to report
(Unscheduled.103). A release is now a `v<version>` tag, built and published by CI only when
the tag names the version `pyproject.toml` carries (ADR-0115).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from claude_delegate_local import doctor

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def check(tag: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "release_check.py"), tag],
                          capture_output=True, text=True, check=False)


def test_the_version_is_a_real_one():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION
    assert VERSION != "0.0.0", "a release would publish a placeholder"


def test_a_tag_naming_the_version_is_accepted():
    assert check(f"v{VERSION}").returncode == 0


@pytest.mark.parametrize("tag", ["v9.9.9", VERSION, f"v{VERSION}-rc1", ""])
def test_any_other_tag_is_refused_and_says_why(tag):
    """The negative control: a mistyped tag must stop the release rather than publish a
    package whose version disagrees with its own name."""
    done = check(tag)
    assert done.returncode == 1
    assert VERSION in done.stderr


def test_the_doctor_reports_the_installed_version(monkeypatch):
    monkeypatch.setattr(doctor.metadata, "version", lambda name: "0.6.0")
    found = doctor.check_version()
    assert found.verdict == doctor.OK
    assert "0.6.0" in found.detail


@pytest.mark.parametrize("installed", ["0.0.0", None])
def test_the_doctor_warns_on_a_placeholder_or_a_missing_install(monkeypatch, installed):
    """An editable install keeps the version it was installed with, so after a bump it
    reads the old one until reinstalled -- worth a line, never a failure."""
    def version(name):
        if installed is None:
            raise metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(doctor.metadata, "version", version)
    found = doctor.check_version()
    assert found.verdict == doctor.WARN
    assert found.remedy


def test_the_release_workflow_checks_the_tag_before_it_builds():
    """Order matters: a check after the publish step is a report, not a gate."""
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert text.index("release_check.py") < text.index("gh release create")
    assert "transcript.schema.json" in text, "the schema is what the viewer pins"
