"""The two tools stage 4 needs, pinned: cosign and gcrane (manifest
`local-tool.cosign`, `local-tool.gcrane`; design §6.1, D21).

Each is downloaded from its project's GitHub release at a pinned version, and
the bytes are checked against a pinned SHA-256 **before anything is written**:
a download whose digest differs is refused, never "retried until it works".
The checksums are the upstream release's own `*checksums.txt` lines, copied
here when the version was pinned; a version bump changes the version and the
four digests in one commit.

A tool already on the person's PATH is not used for stage 4: what verifies a
release's signature must itself be the one this release names.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform as _platform
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

COSIGN_VERSION = "2.4.1"
GCRANE_VERSION = "0.20.2"

_COSIGN = f"https://github.com/sigstore/cosign/releases/download/v{COSIGN_VERSION}"
_GCRANE = f"https://github.com/google/go-containerregistry/releases/download/v{GCRANE_VERSION}"


@dataclass(frozen=True)
class Pinned:
    name: str
    version: str
    url: str
    sha256: str
    #: The binary's path inside a `.tar.gz`; `None` when the download is the binary.
    member: str | None = None


#: (tool, os, arch) -> the pinned download. From cosign_checksums.txt and
#: go-containerregistry's checksums.txt at the versions above.
PINS: dict[tuple[str, str, str], Pinned] = {
    ("cosign", "linux", "amd64"): Pinned(
        "cosign", COSIGN_VERSION, f"{_COSIGN}/cosign-linux-amd64",
        "8b24b946dd5809c6bd93de08033bcf6bc0ed7d336b7785787c080f574b89249b"),
    ("cosign", "linux", "arm64"): Pinned(
        "cosign", COSIGN_VERSION, f"{_COSIGN}/cosign-linux-arm64",
        "3b2e2e3854d0356c45fe6607047526ccd04742d20bd44afb5be91fa2a6e7cb4a"),
    ("cosign", "darwin", "amd64"): Pinned(
        "cosign", COSIGN_VERSION, f"{_COSIGN}/cosign-darwin-amd64",
        "666032ca283da92b6f7953965688fd51200fdc891a86c19e05c98b898ea0af4e"),
    ("cosign", "darwin", "arm64"): Pinned(
        "cosign", COSIGN_VERSION, f"{_COSIGN}/cosign-darwin-arm64",
        "13343856b69f70388c4fe0b986a31dde5958e444b41be22d785d3dc5e1a9cc62"),
    ("gcrane", "linux", "amd64"): Pinned(
        "gcrane", GCRANE_VERSION, f"{_GCRANE}/go-containerregistry_Linux_x86_64.tar.gz",
        "c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b", "gcrane"),
    ("gcrane", "linux", "arm64"): Pinned(
        "gcrane", GCRANE_VERSION, f"{_GCRANE}/go-containerregistry_Linux_arm64.tar.gz",
        "aff0db48825124c9331ea310057214bd4e92c01aa2e414d539e9659841d9422a", "gcrane"),
    ("gcrane", "darwin", "amd64"): Pinned(
        "gcrane", GCRANE_VERSION, f"{_GCRANE}/go-containerregistry_Darwin_x86_64.tar.gz",
        "ae2677fc68b05ee3a63fe7b1d599aa4a554610b9f9da499a0c39669f446d29ed", "gcrane"),
    ("gcrane", "darwin", "arm64"): Pinned(
        "gcrane", GCRANE_VERSION, f"{_GCRANE}/go-containerregistry_Darwin_arm64.tar.gz",
        "b47a8291d1069656bcfb8346dc9494f03e734d7a4058961fa53f0dfc9cb41abb", "gcrane"),
}

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


class ChecksumMismatch(RuntimeError):
    """A download's SHA-256 is not the pinned one: nothing was written."""


def this_platform() -> tuple[str, str]:
    system = _platform.system().lower()
    machine = _ARCH.get(_platform.machine().lower(), _platform.machine().lower())
    return system, machine


def pin_for(name: str, system: tuple[str, str] | None = None) -> Pinned:
    os_name, arch = system or this_platform()
    try:
        return PINS[(name, os_name, arch)]
    except KeyError:
        raise LookupError(f"no pinned {name} for {os_name}/{arch}; this release supports "
                          f"{sorted({(o, a) for n, o, a in PINS if n == name})}") from None


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path("~/.cache").expanduser())
    return Path(base) / "swarmcloud" / "tools"


def _fetch(url: str) -> bytes:
    import requests

    response = requests.get(url, timeout=300)
    response.raise_for_status()
    return response.content


def checked(pin: Pinned, data: bytes) -> bytes:
    """`data` if its SHA-256 is the pinned one; else `ChecksumMismatch`."""
    found = hashlib.sha256(data).hexdigest()
    if found != pin.sha256:
        raise ChecksumMismatch(
            f"{pin.name} {pin.version} from {pin.url} has SHA-256 {found}, not the pinned {pin.sha256}; "
            "refusing it (nothing was written)"
        )
    return data


def _binary(pin: Pinned, data: bytes) -> bytes:
    if pin.member is None:
        return data
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.extractfile(pin.member)
        if member is None:
            raise LookupError(f"{pin.url} holds no {pin.member}")
        return member.read()


def ensure(name: str, *, directory: Path | None = None, fetch: Callable[[str], bytes] = _fetch,
           system: tuple[str, str] | None = None) -> Path:
    """The path of the pinned `name`, downloaded and checked on first use.

    The archive's digest is recorded beside the binary; a cached binary whose
    record is not the pinned digest is downloaded again."""
    pin = pin_for(name, system)
    where = directory or cache_dir()
    target = where / f"{pin.name}-{pin.version}"
    record = target.with_name(target.name + ".sha256")
    if target.is_file() and record.is_file() and record.read_text().strip() == pin.sha256:
        return target
    binary = _binary(pin, checked(pin, fetch(pin.url)))
    where.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(binary)
    partial.chmod(0o755)
    partial.replace(target)
    record.write_text(pin.sha256 + "\n")
    return target
