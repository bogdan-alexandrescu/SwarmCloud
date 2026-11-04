"""CI installs trivy from its GitHub release at a recorded sha256, never through trivy's own install.sh.

From v0.74.0 trivy's contrib/install.sh downloads the tarball from get.trivy.dev. On 3 November 2026 (from about
21:55 universal time) that name stopped resolving, and every CI and release job that installed trivy failed before
it scanned anything. scripts/install-trivy.sh downloads the same tarball from the GitHub release and refuses one
whose sha256 is not the recorded one.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "install-trivy.sh"
CI_FILES = sorted((REPO / ".github" / "workflows").glob("*.yml")) + sorted(
    (REPO / ".github" / "actions").glob("*/action.yml")
)


def _pinned_versions() -> set[str]:
    """Every TRIVY_VERSION a workflow sets, at any level."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "TRIVY_VERSION" and isinstance(value, (str, int, float)):
                    found.add(str(value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for path in CI_FILES:
        walk(yaml.safe_load(path.read_text()))
    return found


def _recorded_assets() -> set[str]:
    text = SCRIPT.read_text()
    block = re.search(r"^TRIVY_SHA256=\((.*?)^\)", text, re.S | re.M)
    assert block, "install-trivy.sh has no TRIVY_SHA256 table"
    return {row.split()[1] for row in re.findall(r'"([^"]+)"', block.group(1))}


def test_no_workflow_or_action_runs_trivys_own_installer() -> None:
    # MUTATION: put the `curl .../contrib/install.sh | sh` line back in any workflow and this fails.
    offenders = [str(p.relative_to(REPO)) for p in CI_FILES if "contrib/install.sh" in p.read_text()]
    assert offenders == [], offenders


def test_every_file_that_pins_trivy_installs_it_through_the_script() -> None:
    # MUTATION: replace one file's `scripts/install-trivy.sh` call with any other install and this fails.
    pinned = [p for p in CI_FILES if re.search(r"^\s*TRIVY_VERSION:", p.read_text(), re.M)]
    installers = [p for p in CI_FILES if "install-trivy.sh" in p.read_text()]
    assert installers, "no workflow installs trivy through scripts/install-trivy.sh"
    for path in pinned:
        text = path.read_text()
        uses_action = "./.github/actions/release-images" in text or "./.github/actions/release-promote" in text
        assert "install-trivy.sh" in text or uses_action, f"{path.relative_to(REPO)} pins trivy but installs it otherwise"


def test_every_pinned_version_has_a_recorded_checksum_for_both_runner_kinds() -> None:
    # MUTATION: bump TRIVY_VERSION in a workflow without adding its two rows and this fails.
    versions = _pinned_versions()
    assert versions, "no workflow pins TRIVY_VERSION"
    assets = _recorded_assets()
    for version in versions:
        for arch in ("64bit", "ARM64"):
            assert f"trivy_{version}_Linux-{arch}.tar.gz" in assets, (version, arch)


def _run(tmp_path: Path, version: str, payload: bytes) -> subprocess.CompletedProcess[str]:
    """Run the script with stand-ins for uname and curl, so nothing is downloaded."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "uname").write_text('#!/bin/sh\ncase "$1" in -s) echo Linux ;; *) echo x86_64 ;; esac\n')
    body = tmp_path / "payload"
    body.write_bytes(payload)
    # Writes the payload to the -o path, as curl would write the download.
    (fake / "curl").write_text(
        '#!/bin/sh\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f'cp "{body}" "$out"\n'
    )
    for name in ("uname", "curl"):
        os.chmod(fake / name, 0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "TRIVY_VERSION": version, "NO_COLOR": "1"}
    return subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "dest")], capture_output=True, text=True, env=env, check=False
    )


def test_a_tarball_whose_checksum_is_not_the_recorded_one_is_refused(tmp_path: Path) -> None:
    # MUTATION: drop the sha256 comparison and this fails (nothing is refused, or tar fails on the junk instead).
    result = _run(tmp_path, "0.74.0", b"not the trivy release")
    assert result.returncode != 0
    assert "refusing to install it" in result.stderr, result.stderr
    assert not (tmp_path / "dest" / "trivy").exists()


def test_a_version_with_no_recorded_checksum_is_refused_before_any_download(tmp_path: Path) -> None:
    # MUTATION: install an unrecorded version unchecked and this fails.
    result = _run(tmp_path, "0.0.1", b"anything")
    assert result.returncode != 0
    assert "no sha256 recorded" in result.stderr, result.stderr
    assert not (tmp_path / "dest").exists()
