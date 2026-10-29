"""The pinned tools stage 4 uses: cosign and gcrane (DAYONE-ENGINE-3; manifest
`local-tool.cosign`, `local-tool.gcrane`; design §6.1, D21).

No network: the download is a fake `fetch`.
"""

from __future__ import annotations

import hashlib
import io
import re
import secrets
import tarfile
from pathlib import Path

import pytest

from swarm_install import tools
from swarm_install.tools import ChecksumMismatch, Pinned


def _pin_for(monkeypatch: pytest.MonkeyPatch, data: bytes, member: str | None = None) -> Pinned:
    pin = Pinned("cosign", "9.9.9", "https://example.invalid/cosign", hashlib.sha256(data).hexdigest(), member)
    monkeypatch.setitem(tools.PINS, ("cosign", "linux", "amd64"), pin)
    return pin


def test_download_checked_against_pinned_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = secrets.token_bytes(64)
    pin = _pin_for(monkeypatch, good)
    fetched: list[str] = []

    def tampered(url: str) -> bytes:
        fetched.append(url)
        return good + b"!"

    with pytest.raises(ChecksumMismatch, match="refusing it"):
        tools.ensure("cosign", directory=tmp_path, fetch=tampered, system=("linux", "amd64"))
    assert fetched == [pin.url]
    assert list(tmp_path.iterdir()) == [], "a download whose digest differs writes nothing"

    path = tools.ensure("cosign", directory=tmp_path, fetch=lambda url: good, system=("linux", "amd64"))
    assert path.read_bytes() == good
    assert path.stat().st_mode & 0o111
    assert (tmp_path / "cosign-9.9.9.sha256").read_text().strip() == pin.sha256

    # Cached: no second download while the record is the pinned digest.
    again = tools.ensure("cosign", directory=tmp_path, fetch=lambda url: pytest.fail("downloaded again"),
                         system=("linux", "amd64"))
    assert again == path

    # A cached binary whose record is not the pinned digest is downloaded (and checked) again.
    (tmp_path / "cosign-9.9.9.sha256").write_text("0" * 64 + "\n")
    with pytest.raises(ChecksumMismatch):
        tools.ensure("cosign", directory=tmp_path, fetch=tampered, system=("linux", "amd64"))


def test_archive_checked_before_the_binary_is_taken_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = secrets.token_bytes(32)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("gcrane")
        info.size = len(binary)
        archive.addfile(info, io.BytesIO(binary))
    data = buffer.getvalue()
    pin = Pinned("gcrane", "9.9.9", "https://example.invalid/gcrane.tar.gz", hashlib.sha256(data).hexdigest(),
                 "gcrane")
    monkeypatch.setitem(tools.PINS, ("gcrane", "darwin", "arm64"), pin)
    path = tools.ensure("gcrane", directory=tmp_path, fetch=lambda url: data, system=("darwin", "arm64"))
    assert path.read_bytes() == binary


def test_every_platform_pinned_with_a_sha256() -> None:
    for name in ("cosign", "gcrane"):
        for system in (("linux", "amd64"), ("linux", "arm64"), ("darwin", "amd64"), ("darwin", "arm64")):
            pin = tools.pin_for(name, system)
            assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256), (name, system)
            assert f"v{pin.version}/" in pin.url, "the URL names the pinned version"
    with pytest.raises(LookupError, match="no pinned cosign"):
        tools.pin_for("cosign", ("windows", "amd64"))
