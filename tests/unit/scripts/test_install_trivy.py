"""scripts/install-trivy.sh installs trivy from its GitHub release at a recorded sha256, and refuses anything else.

From v0.74.0 trivy's contrib/install.sh downloads the tarball from get.trivy.dev. On 3 November 2026 (from about
21:55 universal time) that name stopped resolving, and every CI and release job that installed trivy failed before
it scanned anything. scripts/install-trivy.sh downloads the same tarball from the GitHub release and refuses one
whose sha256 is not the recorded one.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "install-trivy.sh"


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
