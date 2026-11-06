"""`install/install.sh` installs a pinned uv when there is none
(DAYONE-INSTALL-TOOLS-AUTO; part of #2688): a new company needs only curl and
a Google sign-in, so neither uv nor Python is a prerequisite.

No network: a fake `curl` serves the release home and the uv release from a
directory, and the uv tarball is a fake whose pinned digest is written into a
copy of the script (the real digests are the release's own; nothing here can
fetch them).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "install" / "install.sh"
VERSION = "2026.10.1"
DISTS = ("swarm_mcp", "swarm_install", "swarm_common", "swarm_redaction")
UV_TARGETS = {
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-musl",
    ("Linux", "arm64"): "aarch64-unknown-linux-musl",
}
TARGET = UV_TARGETS.get((platform.system(), platform.machine()))

pytestmark = pytest.mark.skipif(TARGET is None or shutil.which("tar") is None,
                                reason="no pinned uv for this platform")

FAKE_CURL = r'''#!{python}
"""A fake curl: serves https://<host>/<path> from FAKE_ROOT/<host>/<path>."""
import os, sys

args = sys.argv[1:]
out = args[args.index("-o") + 1] if "-o" in args else None
url = args[-1]
with open(os.environ["FAKE_CURL_LOG"], "a") as fh:
    fh.write(url + "\n")
path = os.path.join(os.environ["FAKE_ROOT"], url.split("://", 1)[1])
if not os.path.isfile(path):
    sys.exit(22)
data = open(path, "rb").read()
if out:
    open(out, "wb").write(data)
else:
    sys.stdout.buffer.write(data)
'''

FAKE_UV = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_UV_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
'''


def _uv_url() -> str:
    pinned = re.search(r'^UV_VERSION="([^"]+)"$', INSTALL_SH.read_text(), re.M)
    assert pinned is not None
    return f"https://github.com/astral-sh/uv/releases/download/{pinned.group(1)}/uv-{TARGET}.tar.gz"


def _uv_tarball() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("uv", "uvx"):
            body = FAKE_UV.replace("{python}", sys.executable).encode()
            info = tarfile.TarInfo(f"uv-{TARGET}/{name}")
            info.size, info.mode = len(body), 0o755
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _sandbox(tmp_path: Path, *, uv_on_path: bool, tamper: bool = False) -> dict[str, str]:
    root = tmp_path / "web"
    release = root / "storage.googleapis.com" / "swarmcloud" / "releases" / VERSION
    release.mkdir(parents=True)
    sums = []
    for dist in DISTS:
        wheel = f"{dist}-{VERSION}-py3-none-any.whl"
        data = secrets.token_bytes(32)
        (release / wheel).write_bytes(data)
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {wheel}")
    (release / "SHA256SUMS").write_text("\n".join(sums) + "\n")

    tarball = _uv_tarball()
    served = root / _uv_url().split("://", 1)[1]
    served.parent.mkdir(parents=True)
    served.write_bytes(tarball + (b"\0" if tamper else b""))

    # The script, with this platform's pinned digest replaced by the fake tarball's.
    variable = "UV_SHA256_" + TARGET.upper().replace("-", "_")
    script, count = re.subn(rf'^{variable}="[0-9a-f]{{64}}"$',
                            f'{variable}="{hashlib.sha256(tarball).hexdigest()}"',
                            INSTALL_SH.read_text(), flags=re.M)
    assert count == 1, f"install.sh pins {variable} once"
    (tmp_path / "install.sh").write_text(script)

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "curl").write_text(FAKE_CURL.replace("{python}", sys.executable))
    (bindir / "curl").chmod(0o755)
    if uv_on_path:
        (bindir / "uv").write_text(FAKE_UV.replace("{python}", sys.executable))
        (bindir / "uv").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    for log in ("curl.log", "uv.log"):
        (tmp_path / log).write_text("")
    return {"PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home),
            "TMPDIR": str(tmp_path), "SWARMCLOUD_VERSION": VERSION, "FAKE_ROOT": str(root),
            "FAKE_CURL_LOG": str(tmp_path / "curl.log"), "FAKE_UV_LOG": str(tmp_path / "uv.log")}


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], list[str], list[list[str]]]:
    if shutil.which("uv", path="/usr/bin:/bin"):
        pytest.skip("a real uv in /usr/bin or /bin would be found first")
    proc = subprocess.run(["sh", str(tmp_path / "install.sh")], env=env, capture_output=True, text=True,
                          timeout=120, check=False)
    urls = (tmp_path / "curl.log").read_text().split()
    calls = [json.loads(line) for line in (tmp_path / "uv.log").read_text().splitlines() if line.strip()]
    return proc, urls, calls


def test_missing_uv_installed_pinned_into_local_bin(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, uv_on_path=False)
    proc, urls, calls = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert _uv_url() in urls, "the pinned GitHub release tarball, not a remote install script"
    assert not [u for u in urls if u.endswith(".sh")], "no remote script is fetched"
    local_bin = Path(env["HOME"]) / ".local" / "bin"
    for tool in ("uv", "uvx"):
        assert os.access(local_bin / tool, os.X_OK), tool
    (call,) = calls
    assert call[:2] == ["tool", "install"]
    assert "--python" in call, "uv brings a managed Python: Python is never a prerequisite"
    assert "uv is not installed: installing the pinned uv" in proc.stderr


def test_uv_tarball_that_does_not_match_installs_nothing(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, uv_on_path=False, tamper=True)
    proc, _, calls = _run(tmp_path, env)
    assert proc.returncode == 1
    assert "does not match its pinned SHA-256" in proc.stderr
    assert not (Path(env["HOME"]) / ".local" / "bin" / "uv").exists()
    assert calls == []


def test_uv_on_path_is_used_and_nothing_fetched(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, uv_on_path=True)
    proc, urls, calls = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert _uv_url() not in urls
    assert not (Path(env["HOME"]) / ".local" / "bin" / "uv").exists()
    assert len(calls) == 1


def test_every_platform_pins_a_uv_digest() -> None:
    text = INSTALL_SH.read_text()
    for target in set(UV_TARGETS.values()):
        variable = "UV_SHA256_" + target.upper().replace("-", "_")
        assert re.search(rf'^{variable}="[0-9a-f]{{64}}"$', text, re.M), variable
    assert "| sh" not in text.split("set -eu", 1)[1], "never pipes a remote script to a shell"
