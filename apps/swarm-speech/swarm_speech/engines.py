"""The speech-to-text and text-to-speech engines, behind one interface each
(docs/design/voice-it-own-speech.md §3.3, §5.2).

The server (server.py) sees only `SpeechToText` and `TextToSpeech`. The real
ones are Qwen3-ASR through vLLM's streaming path and Kokoro on the machine's
L4; the unit tests run the server with fakes behind the same two interfaces,
with no GPU and no weights. Every call here blocks (it is the GPU's work): the
server runs each in a thread, one at a time per engine.

Audio crosses this boundary as bytes of 16-bit little-endian mono PCM: 16 kHz
into speech-to-text, 24 kHz out of text-to-speech, the socket's own formats,
so nothing between the socket and the model resamples. torch and numpy are
imported only inside the real engines, so the server and its tests import
without them.
"""

from __future__ import annotations

import array
import sys
from pathlib import Path
from typing import Iterator, Protocol

STT_RATE = 16_000
TTS_RATE = 24_000

# vLLM pre-allocates this share of the L4's 24 GB for the ASR model and its KV
# cache. 0.6B's weights are about 1.5 GB; Kokoro (82M) needs under 1 GB beside
# it, and CUDA's own context about 0.5 GB per process. 0.5 leaves about 12 GB
# for Kokoro and headroom. Not yet measured on an L4: the machine lane's M2
# measures the GPU memory both use and sets this from that measurement.
GPU_MEMORY_UTILIZATION = 0.5
# Qwen3-ASR decodes the audio so far each time this much new audio has
# arrived; it is the partials' cadence. The upstream streaming demo's default
# is 2.0 s; 1.0 s halves the wait for the first partial at twice the decodes
# of a short utterance. M4 measures the cadence and real-time factor.
CHUNK_SECONDS = 1.0
# Upper bound on one decode's output; a 60 s utterance (server.MAX_UTTERANCE_SECONDS)
# of fast speech is about 200 words, about 300 tokens.
MAX_NEW_TOKENS = 512
# Kokoro speaks sentence by sentence: the text is split after ., ! or ? and a
# space, so the first sentence's audio streams while the next is synthesised.
SENTENCE_SPLIT = r"(?<=[.!?])\s+"


class SttStream(Protocol):
    def feed(self, pcm: bytes) -> str:
        """Add 16 kHz PCM; the words heard so far (a partial)."""

    def finish(self) -> str:
        """Flush what is buffered; the final words."""


class SpeechToText(Protocol):
    def load(self, model_dir: Path) -> None: ...

    def begin(self) -> SttStream: ...


class TextToSpeech(Protocol):
    def load(self, model_dir: Path, *, repo: str, voices: tuple[str, ...]) -> None: ...

    def synthesize(self, text: str, voice: str) -> Iterator[bytes]:
        """24 kHz PCM, one chunk per sentence, in order."""


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """Kokoro's 24 kHz output as 16 kHz input for the warm-up's round trip:
    linear interpolation at two output samples per three input samples."""
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        src.byteswap()
    out = array.array("h")
    n = len(src)
    for i in range((n * 2) // 3):
        pos = i * 1.5
        left = int(pos)
        frac = pos - left
        right = min(left + 1, n - 1)
        out.append(int(round(src[left] * (1.0 - frac) + src[right] * frac)))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


class _QwenStream:
    def __init__(self, model, state) -> None:
        self._model = model
        self._state = state

    def feed(self, pcm: bytes) -> str:
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._model.streaming_transcribe(samples, self._state)
        return self._state.text or ""

    def finish(self) -> str:
        self._model.finish_streaming_transcribe(self._state)
        return self._state.text or ""


class QwenAsr:
    """Qwen3-ASR through `qwen-asr[vllm]`'s streaming path (vLLM backend only)."""

    def __init__(self) -> None:
        self._model = None

    def load(self, model_dir: Path) -> None:
        from qwen_asr import Qwen3ASRModel

        self._model = Qwen3ASRModel.LLM(model=str(model_dir),
                                        gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
                                        max_new_tokens=MAX_NEW_TOKENS)

    def begin(self) -> SttStream:
        if self._model is None:
            raise RuntimeError("the speech-to-text model is not loaded")
        return _QwenStream(self._model, self._model.init_streaming_state(chunk_size_sec=CHUNK_SECONDS))


class KokoroTts:
    """Kokoro-82M from its fetched directory: config.json, the weights and
    voices/<voice>.pt, every one checked by models.fetch before this runs."""

    def __init__(self) -> None:
        self._pipelines: dict[str, object] = {}
        self._voices: dict[str, str] = {}

    def load(self, model_dir: Path, *, repo: str, voices: tuple[str, ...]) -> None:
        # `repo` is the model's Hugging Face id: Kokoro keys its weights' layout by it.
        import torch
        from kokoro import KModel, KPipeline

        weights = sorted(model_dir.glob("*.pth"))
        if len(weights) != 1:
            raise RuntimeError(f"expected one .pth weights file in the text-to-speech model, found {len(weights)}")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        kmodel = KModel(repo_id=repo, config=str(model_dir / "config.json"), model=str(weights[0]))
        kmodel = kmodel.to(device).eval()
        for voice in voices:
            # A voice's first letter is its language: "a" American, "b" British English.
            lang = voice[0]
            if lang not in self._pipelines:
                self._pipelines[lang] = KPipeline(lang_code=lang, repo_id=repo, model=kmodel, device=device)
            path = str(model_dir / "voices" / f"{voice}.pt")
            self._pipelines[lang].load_voice(path)
            self._voices[voice] = path

    def synthesize(self, text: str, voice: str) -> Iterator[bytes]:
        import torch

        path = self._voices.get(voice)
        if path is None:
            raise KeyError(voice)
        pipeline = self._pipelines[voice[0]]
        for result in pipeline(text, voice=path, split_pattern=SENTENCE_SPLIT):
            audio = result.audio
            if audio is None or audio.numel() == 0:
                continue
            pcm = (audio.detach().clamp(-1.0, 1.0) * 32767.0).to(torch.int16).cpu().numpy()
            yield pcm.astype("<i2").tobytes()

