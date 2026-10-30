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
import zipfile
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


# -- DAYONE-INSTALL-TOOLS-AUTO: terraform, kubectl, jq and the Google Cloud SDK --

PLATFORMS = (("linux", "amd64"), ("linux", "arm64"), ("darwin", "amd64"), ("darwin", "arm64"))


def test_preflight_tools_pinned_for_every_platform() -> None:
    assert tools.TERRAFORM_VERSION == "1.16.2"
    major, minor, _ = (int(p) for p in tools.KUBECTL_VERSION.split("."))
    assert (major, minor) >= (1, 30), "kubectl's floor (lesson 12)"
    hosts = {"terraform": "https://releases.hashicorp.com/terraform/1.16.2/",
             "kubectl": f"https://dl.k8s.io/release/v{tools.KUBECTL_VERSION}/bin/",
             "jq": f"https://github.com/jqlang/jq/releases/download/jq-{tools.JQ_VERSION}/"}
    for name, prefix in hosts.items():
        digests = set()
        for system in PLATFORMS:
            pin = tools.pin_for(name, system)
            assert pin.url.startswith(prefix), (name, system)
            assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256), (name, system)
            digests.add(pin.sha256)
        assert len(digests) == 4, f"{name}: one digest per platform"
    assert tools.pin_for("terraform", ("darwin", "arm64")).member == "terraform"
    for system in PLATFORMS:
        pin = tools.SDK_PINS[system]
        assert f"google-cloud-cli-{tools.GCLOUD_VERSION}-" in pin.url, "a versioned archive, never 'latest'"
        assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256)


def test_terraform_zip_checked_then_unpacked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = secrets.token_bytes(48)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("terraform", binary)
        archive.writestr("LICENSE.txt", "licence")
    data = buffer.getvalue()
    pin = Pinned("terraform", "1.16.2", "https://example.invalid/terraform_1.16.2_linux_amd64.zip",
                 hashlib.sha256(data).hexdigest(), "terraform")
    monkeypatch.setitem(tools.PINS, ("terraform", "linux", "amd64"), pin)
    with pytest.raises(ChecksumMismatch):
        tools.ensure("terraform", directory=tmp_path, fetch=lambda url: data + b"x", system=("linux", "amd64"))
    assert list(tmp_path.iterdir()) == []
    path = tools.ensure("terraform", directory=tmp_path, fetch=lambda url: data, system=("linux", "amd64"))
    assert path.read_bytes() == binary and path.stat().st_mode & 0o111


def test_link_and_bin_dir_first_on_path(tmp_path: Path) -> None:
    first, second = tmp_path / "terraform-1", tmp_path / "terraform-2"
    first.write_text("1")
    second.write_text("2")
    bin_dir = tmp_path / "bin"
    env = {"PATH": f"/usr/bin:{bin_dir}:/bin"}
    assert tools.use_bin_dir(bin_dir, env) is False and env["PATH"] == f"/usr/bin:{bin_dir}:/bin"
    tools.link("terraform", first, directory=bin_dir)
    link = tools.link("terraform", second, directory=bin_dir)
    assert link.read_text() == "2"
    assert tools.use_bin_dir(bin_dir, env) is True
    assert env["PATH"] == f"{bin_dir}:/usr/bin:/bin"


def _sdk_archive(gcloud: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body, mode in (("google-cloud-sdk/bin/gcloud", gcloud, 0o755),
                                 ("google-cloud-sdk/VERSION", b"591.0.0\n", 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), mode
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def test_sdk_checked_before_unpacking_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _sdk_archive(secrets.token_bytes(16))
    pin = Pinned("gcloud", "591.0.0", "https://example.invalid/google-cloud-cli-591.0.0-linux-x86_64.tar.gz",
                 hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(tools.SDK_PINS, ("linux", "amd64"), pin)
    home = tmp_path / "swarm"
    with pytest.raises(ChecksumMismatch):
        tools.install_sdk(home=home, fetch=lambda url: data + b"!", system=("linux", "amd64"))
    assert not home.exists() or list(home.iterdir()) == [], "a tampered archive unpacks nothing"

    gcloud = tools.install_sdk(home=home, fetch=lambda url: data, system=("linux", "amd64"))
    assert gcloud == home / "google-cloud-sdk" / "bin" / "gcloud" and gcloud.stat().st_mode & 0o111
    assert sorted(p.name for p in home.iterdir()) == ["google-cloud-sdk"], "no partial left behind"
    again = tools.install_sdk(home=home, fetch=lambda url: pytest.fail("downloaded again"),
                              system=("linux", "amd64"))
    assert again == gcloud


def test_sdk_never_overwrites_a_tree_it_did_not_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _sdk_archive(b"#!/bin/sh\n")
    monkeypatch.setitem(tools.SDK_PINS, ("darwin", "arm64"),
                        Pinned("gcloud", "591.0.0", "https://example.invalid/sdk.tar.gz",
                               hashlib.sha256(data).hexdigest()))
    theirs = tmp_path / "google-cloud-sdk" / "bin"
    theirs.mkdir(parents=True)
    (theirs / "gcloud").write_text("theirs")
    with pytest.raises(FileExistsError, match="not installed by swarm"):
        tools.install_sdk(home=tmp_path, fetch=lambda url: data, system=("darwin", "arm64"))
    assert (theirs / "gcloud").read_text() == "theirs"
