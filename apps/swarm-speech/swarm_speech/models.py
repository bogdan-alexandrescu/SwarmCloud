"""Which models the server runs, and how their weights reach the disk
(docs/design/voice-it-own-speech.md §5.2, §11.2).

The machine's `swarm-speech-models` metadata, which swarm-api sets from the
settings catalogue at each start (§11.4), reaches the container as the JSON in
SWARM_SPEECH_MODELS:

    {"stt": {"name": "qwen3-asr-0.6b", "hf": "Qwen/Qwen3-ASR-0.6B",
             "revision": "<40 hex>", "sha256": {"<file>": "<64 hex>", ...}},
     "tts": {"name": "kokoro-82m", "hf": "hexgrad/Kokoro-82M",
             "revision": "<40 hex>", "sha256": {...}, "voices": ["af_heart", ...]}}

Every value is checked here before anything is fetched: a name, a Hugging Face
repository, a commit (never a branch, so the weights cannot move under a pin)
and the SHA-256 of EVERY file to fetch. Only the files the list names are
fetched, so every byte on disk was checked; a model whose list is empty is
refused rather than fetched unchecked. Public weights need no credential.

A model lives in `<root>/<name>@<revision>/`, written to a temporary sibling
and renamed only after every checksum matched, with a `.complete` marker: a
crash mid-fetch leaves no directory that looks finished, and a model already on
disk is not fetched again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
# Kokoro's voice names: a two-letter language and gender prefix, then a name.
_VOICE = re.compile(r"^[a-z]{2}_[a-z]{1,32}$")

COMPLETE = ".complete"


class ModelConfigError(ValueError):
    """SWARM_SPEECH_MODELS is missing or names something the server refuses."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    hf: str
    revision: str
    sha256: dict[str, str]
    voices: tuple[str, ...] = ()

    @property
    def dirname(self) -> str:
        return f"{self.name}@{self.revision}"


@dataclass(frozen=True)
class Models:
    stt: ModelSpec
    tts: ModelSpec


def _spec(role: str, raw: object, *, voices: bool) -> ModelSpec:
    if not isinstance(raw, dict):
        raise ModelConfigError(f"{role}: not an object")
    allowed = {"name", "hf", "revision", "sha256"} | ({"voices"} if voices else set())
    extra = sorted(set(raw) - allowed)
    if extra:
        raise ModelConfigError(f"{role}: unknown fields {extra}")
    name, hf, revision, sums = raw.get("name"), raw.get("hf"), raw.get("revision"), raw.get("sha256")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ModelConfigError(f"{role}.name is not a catalogue name")
    if not isinstance(hf, str) or not _REPO.match(hf):
        raise ModelConfigError(f"{role}.hf is not a Hugging Face repository")
    if not isinstance(revision, str) or not _REVISION.match(revision):
        raise ModelConfigError(f"{role}.revision is not a 40-hex commit")
    if not isinstance(sums, dict) or not sums:
        raise ModelConfigError(f"{role}.sha256 is empty: no file would be checked")
    for path, digest in sums.items():
        if not isinstance(path, str) or not _FILE.match(path) or ".." in path.split("/"):
            raise ModelConfigError(f"{role}.sha256 names a file that is not a plain relative path")
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise ModelConfigError(f"{role}.sha256[{path}] is not a SHA-256")
    names: tuple[str, ...] = ()
    if voices:
        listed = raw.get("voices")
        if not isinstance(listed, list) or not listed:
            raise ModelConfigError(f"{role}.voices is empty")
        for voice in listed:
            if not isinstance(voice, str) or not _VOICE.match(voice):
                raise ModelConfigError(f"{role}.voices holds a name that is not a voice")
            if f"voices/{voice}.pt" not in sums:
                raise ModelConfigError(f"{role}.voices: voices/{voice}.pt has no SHA-256")
        names = tuple(listed)
    return ModelSpec(name=name, hf=hf, revision=revision, sha256=dict(sums), voices=names)


def parse_models(text: str | None) -> Models:
    if not text or not text.strip():
        raise ModelConfigError("SWARM_SPEECH_MODELS is not set")
    try:
        raw = json.loads(text)
    except ValueError:
        raise ModelConfigError("SWARM_SPEECH_MODELS is not JSON") from None
    if not isinstance(raw, dict) or set(raw) != {"stt", "tts"}:
        raise ModelConfigError("SWARM_SPEECH_MODELS must hold exactly stt and tts")
    return Models(stt=_spec("stt", raw["stt"], voices=False), tts=_spec("tts", raw["tts"], voices=True))


# (repository, file, revision, directory) -> the fetched file's path.
Download = Callable[[str, str, str, Path], Path]


def hf_download(repo: str, filename: str, revision: str, directory: Path) -> Path:
    """One file of a public Hugging Face repository at a pinned commit."""
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(repo_id=repo, filename=filename, revision=revision,
                                local_dir=str(directory)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class ChecksumMismatch(RuntimeError):
    """A fetched file is not the file the catalogue pinned."""


def is_present(root: Path, spec: ModelSpec) -> bool:
    return (root / spec.dirname / COMPLETE).is_file()


def fetch(root: Path, spec: ModelSpec, download: Download = hf_download) -> Path:
    """The model's directory, fetched and checked unless it is already there."""
    final = root / spec.dirname
    if is_present(root, spec):
        return final
    staging = root / f".{spec.dirname}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for name, expected in sorted(spec.sha256.items()):
            got = download(spec.hf, name, spec.revision, staging)
            if _sha256(got) != expected:
                # The name only: never the bytes, and nothing from the remote.
                raise ChecksumMismatch(f"{spec.name}: {name} does not match its pinned SHA-256")
            wanted = staging / name
            if got.resolve() != wanted.resolve():
                wanted.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(got), str(wanted))
        (staging / COMPLETE).write_text(spec.revision + "\n", encoding="utf-8")
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final
