"""A fake speech-to-text and text-to-speech behind the server's interfaces
(apps/swarm-speech/swarm_speech/engines.py), and the specs and downloads that
go with them. No GPU, no weights, no network."""

from __future__ import annotations

import hashlib
import secrets
import sys
import threading
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-speech")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_speech.boot import WARMUP_SENTENCE  # noqa: E402
from swarm_speech.models import ModelSpec, Models  # noqa: E402

# A word of the fake transcript is revealed per 100 ms of 16 kHz audio fed.
BYTES_PER_WORD = 3200


class FakeStream:
    def __init__(self, stt: "FakeStt") -> None:
        self._stt = stt
        self.fed = 0

    def feed(self, pcm: bytes) -> str:
        self._stt.fed.append(len(pcm))
        if self._stt.fail_feed:
            raise RuntimeError("decoder broke")
        self.fed += len(pcm)
        return " ".join(self._stt.transcript.split()[: self.fed // BYTES_PER_WORD])

    def finish(self) -> str:
        return self._stt.transcript


class FakeStt:
    def __init__(self, transcript: str = WARMUP_SENTENCE) -> None:
        self.transcript = transcript
        self.loaded: Path | None = None
        self.fed: list[int] = []
        self.fail_feed = False
        self.fail_load = False

    def load(self, model_dir: Path) -> None:
        if self.fail_load:
            raise RuntimeError("CUDA out of memory")
        self.loaded = model_dir

    def begin(self) -> FakeStream:
        return FakeStream(self)


class FakeTts:
    """Each sentence is 0.5 s of 24 kHz audio (24,000 bytes). With `gate` set,
    every sentence after the first waits for it, so a test can cancel mid-speech."""

    SENTENCE_BYTES = 24_000

    def __init__(self, *, silent: bool = False) -> None:
        self.silent = silent
        self.loaded: tuple[Path, str, tuple[str, ...]] | None = None
        self.gate: threading.Event | None = None
        self.spoken: list[tuple[str, str]] = []

    def load(self, model_dir: Path, *, repo: str, voices: tuple[str, ...]) -> None:
        self.loaded = (model_dir, repo, voices)

    def synthesize(self, text: str, voice: str):
        self.spoken.append((text, voice))
        if self.silent:
            return
        for n, _sentence in enumerate(s for s in text.split(". ") if s):
            if n and self.gate is not None:
                self.gate.wait(5)
            yield b"\x01\x00" * (self.SENTENCE_BYTES // 2)


def blob(name: str) -> bytes:
    return f"weights of {name}".encode()


def spec(name: str, hf: str, files: list[str], voices: tuple[str, ...] = ()) -> ModelSpec:
    return ModelSpec(name=name, hf=hf, revision=secrets.token_hex(20),
                     sha256={f: hashlib.sha256(blob(f)).hexdigest() for f in files}, voices=voices)


def models() -> Models:
    return Models(
        stt=spec("qwen3-asr-0.6b", "Qwen/Qwen3-ASR-0.6B", ["config.json", "model.safetensors"]),
        tts=spec("kokoro-82m", "hexgrad/Kokoro-82M",
                 ["config.json", "kokoro-v1_0.pth", "voices/af_heart.pt", "voices/bm_george.pt"],
                 voices=("af_heart", "bm_george")))


class FakeDownload:
    """Writes each file's fake bytes, unless told to corrupt one; optionally
    waits on `gate` before its first file so a test can watch the phases."""

    def __init__(self, corrupt: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.corrupt = corrupt
        self.gate: threading.Event | None = None

    def __call__(self, repo: str, filename: str, revision: str, directory: Path) -> Path:
        if self.gate is not None:
            self.gate.wait(5)
        self.calls.append((repo, filename, revision))
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tampered" if filename == self.corrupt else blob(filename))
        return path
