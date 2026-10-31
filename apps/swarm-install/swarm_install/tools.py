"""The local tools the installer fetches itself, pinned: cosign and gcrane
(stage 4; manifest `local-tool.cosign`, `local-tool.gcrane`; design §6.1, D21),
terraform, kubectl and jq (preflight, `local-tool.terraform`, `.kubectl`, `.jq`)
the Google Cloud SDK (`local-tool.gcloud`, on the person's yes;
DAYONE-INSTALL-TOOLS-AUTO) and the gke-gcloud-auth-plugin kubectl execs for a
GKE context (`local-tool.gke-gcloud-auth-plugin`, DAYONE-PREFLIGHT-ASKS), and gh,
the GitHub CLI `swarm addon enable ci` drives (DAYONE-ADDON-2). A new company needs only curl and a Google sign-in.

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
import shutil
import tarfile
import zipfile
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

COSIGN_VERSION = "2.4.1"
GCRANE_VERSION = "0.20.2"
#: Exact: 1.3.6 refuses the state and a newer one diverges plans (lesson 12).
TERRAFORM_VERSION = "1.16.2"
#: The newest 1.36 patch, the one the images carry (docs/versions.md): within
#: one minor of the GKE control plane, as kubectl must be.
KUBECTL_VERSION = "1.36.6"
JQ_VERSION = "1.8.2"
GCLOUD_VERSION = "591.0.0"
#: The GKE auth plugin kubectl execs for a GKE context (CLUSTER-REACH), as
#: GCLOUD_VERSION's component manifest names it: version 0.5.20, build
#: 20261016073856.
GKE_AUTH_PLUGIN_VERSION = "0.5.20"
GKE_AUTH_PLUGIN_BUILD = "20261016073856"
GH_VERSION = "2.103.3"

_COSIGN = f"https://github.com/sigstore/cosign/releases/download/v{COSIGN_VERSION}"
_GCRANE = f"https://github.com/google/go-containerregistry/releases/download/v{GCRANE_VERSION}"
_TERRAFORM = f"https://releases.hashicorp.com/terraform/{TERRAFORM_VERSION}/terraform_{TERRAFORM_VERSION}"
_KUBECTL = f"https://dl.k8s.io/release/v{KUBECTL_VERSION}/bin"
_JQ = f"https://github.com/jqlang/jq/releases/download/jq-{JQ_VERSION}"
_GCLOUD = f"https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-{GCLOUD_VERSION}"
_GH = f"https://github.com/cli/cli/releases/download/v{GH_VERSION}/gh_{GH_VERSION}"
_GKE_AUTH = "https://dl.google.com/dl/cloudsdk/channels/rapid/components/google-cloud-sdk-gke-gcloud-auth-plugin"


@dataclass(frozen=True)
class Pinned:
    name: str
    version: str
    url: str
    sha256: str
    #: The binary's path inside a `.tar.gz` or `.zip`; `None` when the download is the binary.
    member: str | None = None
    #: Cached under its own name (in a versioned directory) rather than as
    #: `<name>-<version>`: another tool execs it from PATH by that exact name.
    exec_name: bool = False


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
    # HashiCorp's terraform_1.16.2_SHA256SUMS (releases.hashicorp.com), read 2026-10-30.
    ("terraform", "linux", "amd64"): Pinned(
        "terraform", TERRAFORM_VERSION, f"{_TERRAFORM}_linux_amd64.zip",
        "0d17011f0c4664539b164b044903d04e296c86c13cb9f28040076c65cfb3985a", "terraform"),
    ("terraform", "linux", "arm64"): Pinned(
        "terraform", TERRAFORM_VERSION, f"{_TERRAFORM}_linux_arm64.zip",
        "c040bd1e3122b4290f70f74288d8c5a54ddd4254a3e52a29e7cc666653f50a0a", "terraform"),
    ("terraform", "darwin", "amd64"): Pinned(
        "terraform", TERRAFORM_VERSION, f"{_TERRAFORM}_darwin_amd64.zip",
        "275f50ed172af2f23e15a2ce7e3251aac8b17b768f2e62eceb81c353cd106abf", "terraform"),
    ("terraform", "darwin", "arm64"): Pinned(
        "terraform", TERRAFORM_VERSION, f"{_TERRAFORM}_darwin_arm64.zip",
        "7c0a0b31c8aa541351369bcf7b62a7289fbc21de7e577669aeba6d42f4e6cc41", "terraform"),
    # dl.k8s.io's own kubectl.sha256 beside each binary (release/v1.36.6/bin/<os>/<arch>/), read 2026-10-30.
    ("kubectl", "linux", "amd64"): Pinned(
        "kubectl", KUBECTL_VERSION, f"{_KUBECTL}/linux/amd64/kubectl",
        "29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634"),
    ("kubectl", "linux", "arm64"): Pinned(
        "kubectl", KUBECTL_VERSION, f"{_KUBECTL}/linux/arm64/kubectl",
        "35507ef408c3d38849e5d3203e18f8e861db822aa664101a9ced0869edd06046"),
    ("kubectl", "darwin", "amd64"): Pinned(
        "kubectl", KUBECTL_VERSION, f"{_KUBECTL}/darwin/amd64/kubectl",
        "2e22d382f2423c88d9d076246f860bb3d5c743dd279ca63e113474bb88382426"),
    ("kubectl", "darwin", "arm64"): Pinned(
        "kubectl", KUBECTL_VERSION, f"{_KUBECTL}/darwin/arm64/kubectl",
        "1cf735c3d35ccfec28d0618784cb06a4614d2c42aade28b6be0eab76a2ffb623"),
    # jq's sha256sum.txt on its jq-1.8.2 GitHub release, read 2026-10-30.
    ("jq", "linux", "amd64"): Pinned(
        "jq", JQ_VERSION, f"{_JQ}/jq-linux-amd64",
        "b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f"),
    ("jq", "linux", "arm64"): Pinned(
        "jq", JQ_VERSION, f"{_JQ}/jq-linux-arm64",
        "8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309"),
    ("jq", "darwin", "amd64"): Pinned(
        "jq", JQ_VERSION, f"{_JQ}/jq-macos-amd64",
        "e94b266e3c26690550006abe63152b782280f4e14374accdf04cbde844f00bc0"),
    ("jq", "darwin", "arm64"): Pinned(
        "jq", JQ_VERSION, f"{_JQ}/jq-macos-arm64",
        "2d75340ba57a4b4b4c8708a21c2dc8e958a48aaa8bba13b27f77f6e4c0eca07e"),
}

#: gh from its GitHub release's gh_2.103.3_checksums.txt, read 2026-10-31 and
#: matched by downloading the linux_amd64 and macOS_arm64 archives that day.
PINS.update({
    ("gh", os_name, arch): Pinned(
        "gh", GH_VERSION, f"{_GH}_{label}_{arch}.{ext}", digest, f"gh_{GH_VERSION}_{label}_{arch}/bin/gh")
    for (os_name, label, arch, ext), digest in (
        (("linux", "linux", "amd64", "tar.gz"), "4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d"),
        (("linux", "linux", "arm64", "tar.gz"), "ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63"),
        (("darwin", "macOS", "amd64", "zip"), "a2f86418f4350ae6e9ec60d5000bdf7665728436946a97fc27fe50e6ae752682"),
        (("darwin", "macOS", "arm64", "zip"), "d700207eae9ae98fe2b89535c2c087c07823d4628c56364695a5a07ef311c062"),
    )
})

#: The gke-gcloud-auth-plugin component for each platform. The digests are the
#: `data.checksum` (SHA-256 of the archive) of each `gke-gcloud-auth-plugin-*`
#: component in Google's published component manifest for this SDK,
#: https://dl.google.com/dl/cloudsdk/channels/rapid/components-v591.0.0.json,
#: read on 2026-10-30 and matched by downloading each archive that day. The
#: manifest's "arm" is arm64 (aarch64) on both systems.
PINS.update({
    ("gke-gcloud-auth-plugin", os_name, arch): Pinned(
        "gke-gcloud-auth-plugin", GKE_AUTH_PLUGIN_VERSION, f"{_GKE_AUTH}-{component}-{GKE_AUTH_PLUGIN_BUILD}.tar.gz",
        digest, "bin/gke-gcloud-auth-plugin", exec_name=True)
    for (os_name, arch), component, digest in (
        (("linux", "amd64"), "linux-x86_64", "7ca2be31d6faa232b733cd3e91b98e182771516732d7230c19196b51cde7f823"),
        (("linux", "arm64"), "linux-arm", "9f53cf95d4bebfe313cd61741d75971b4cd34194fdf00780d754611d22a03528"),
        (("darwin", "amd64"), "darwin-x86_64", "8f6f6a5d67717e018cd352374ea89bf0c29e7812403ee2ec15df3000fc366f29"),
        (("darwin", "arm64"), "darwin-arm", "beb9e6ce595ca4cec5504d7f6cf85ea5863cec3d89a618c6e5249ebe70f295cc"),
    )
})

#: (os, arch) -> the Google Cloud SDK's versioned archive, a whole tree rather
#: than one binary. Google publishes no sums file for the versioned archives
#: (its install page lists digests only for the unversioned "latest" ones), so
#: these are the SHA-256 of each versioned archive as downloaded on 2026-10-30,
#: when the unversioned linux-x86_64 archive (also 591.0.0) matched the page's
#: published 8b8c1be1...ec4326. A bump re-reads the four in one commit.
SDK_PINS: dict[tuple[str, str], Pinned] = {
    ("linux", "amd64"): Pinned(
        "gcloud", GCLOUD_VERSION, f"{_GCLOUD}-linux-x86_64.tar.gz",
        "78c570f5fcfe58a9311bd98c06b75530595e3cae6277aad75a676a39e6ee7b63"),
    ("linux", "arm64"): Pinned(
        "gcloud", GCLOUD_VERSION, f"{_GCLOUD}-linux-arm.tar.gz",
        "f5e5df0433f77c0e7f5df9fd4bce2e46c7b7967e79cf66e31b1164d3d4c86546"),
    ("darwin", "amd64"): Pinned(
        "gcloud", GCLOUD_VERSION, f"{_GCLOUD}-darwin-x86_64.tar.gz",
        "89c3ca942eb609293dff5b549ec6bae5d9ec41633a132aeb8dd8087a915ab3a0"),
    ("darwin", "arm64"): Pinned(
        "gcloud", GCLOUD_VERSION, f"{_GCLOUD}-darwin-arm.tar.gz",
        "85cb726919838faff48bfba7cf676b691b0202bec7c25aabdc116649c6843242"),
}

#: Where the SDK goes on the person's yes (the prompt names this path).
SDK_HOME = "~/.swarm"

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
    if pin.url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                return archive.read(pin.member)
            except KeyError:
                raise LookupError(f"{pin.url} holds no {pin.member}") from None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.extractfile(pin.member)
        if member is None:
            raise LookupError(f"{pin.url} holds no {pin.member}")
        return member.read()


def ensure(name: str, *, directory: Path | None = None, fetch: Callable[[str], bytes] = _fetch,
           system: tuple[str, str] | None = None) -> Path:
    """The path of the pinned `name`, downloaded and checked on first use.

    The archive's digest is recorded beside the binary; a cached binary whose
    record is not the pinned digest is downloaded again. A pin with
    `exec_name` (gke-gcloud-auth-plugin) is `<name>-<version>/<name>`, alone
    in its directory, so the caller can put that directory first on PATH."""
    pin = pin_for(name, system)
    base = directory or cache_dir()
    versioned = base / f"{pin.name}-{pin.version}"
    target = versioned / pin.name if pin.exec_name else versioned
    where = target.parent
    record = versioned.with_name(versioned.name + ".sha256")
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


def bin_dir() -> Path:
    """The directory of `<tool>` links to the pinned binaries, first on the
    engine's PATH (`use_bin_dir`), so its subprocesses run the pinned ones."""
    return cache_dir() / "bin"


def link(name: str, binary: Path, *, directory: Path | None = None) -> Path:
    """`directory/name` -> `binary`, replacing whatever link was there."""
    where = directory or bin_dir()
    where.mkdir(parents=True, exist_ok=True)
    target = where / name
    partial = where / f".{name}.partial"
    if partial.is_symlink() or partial.exists():
        partial.unlink()
    partial.symlink_to(binary)
    partial.replace(target)
    return target


def use_bin_dir(directory: Path | None = None, environ: MutableMapping[str, str] | None = None) -> bool:
    """Put `directory` first on PATH when it holds a link; True when it does."""
    where = directory or bin_dir()
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    if not where.is_dir() or not any(where.iterdir()):
        return False
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != str(where)]
    env["PATH"] = os.pathsep.join([str(where), *parts])
    return True


def _extract(archive: tarfile.TarFile, into: Path) -> None:
    if not hasattr(tarfile, "data_filter"):  # Python 3.11.4; uv's managed Pythons all have it
        raise RuntimeError("this Python cannot extract an archive safely (3.11.4 or newer is needed)")
    archive.extractall(into, filter="data")


def install_sdk(*, home: Path | None = None, fetch: Callable[[str], bytes] = _fetch,
                system: tuple[str, str] | None = None) -> Path:
    """The pinned Google Cloud SDK in `home/google-cloud-sdk`; its `bin/gcloud`.

    The archive is checked before anything is unpacked, unpacked beside the
    target and moved into place whole, with its digest recorded inside it. A
    tree there without that record is the person's own and is never touched."""
    os_name, arch = system or this_platform()
    try:
        pin = SDK_PINS[(os_name, arch)]
    except KeyError:
        raise LookupError(f"no pinned Google Cloud SDK for {os_name}/{arch}; this release supports "
                          f"{sorted(SDK_PINS)}") from None
    where = home or Path(SDK_HOME).expanduser()
    target = where / "google-cloud-sdk"
    record = target / ".swarm-sha256"
    gcloud = target / "bin" / "gcloud"
    if target.exists():
        if not record.is_file():
            raise FileExistsError(f"{target} exists and was not installed by swarm; move it aside or put its "
                                  "bin directory on PATH, then run swarm install again")
        if record.read_text().strip() == pin.sha256 and gcloud.is_file():
            return gcloud
    data = checked(pin, fetch(pin.url))
    partial = where / ".google-cloud-sdk.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        _extract(archive, partial)
    unpacked = partial / "google-cloud-sdk"
    if not (unpacked / "bin" / "gcloud").is_file():
        shutil.rmtree(partial)
        raise LookupError(f"{pin.url} holds no google-cloud-sdk/bin/gcloud")
    (unpacked / ".swarm-sha256").write_text(pin.sha256 + "\n")
    if target.exists():  # an older pinned SDK this installer put there (its record says so)
        retired = where / ".google-cloud-sdk.old"
        if retired.exists():
            shutil.rmtree(retired)
        target.replace(retired)
        shutil.rmtree(retired)
    unpacked.replace(target)
    shutil.rmtree(partial)
    return gcloud
