"""The speech-to-text and text-to-speech engines, behind one interface each
(docs/design/voice-it-own-speech.md §3.3, §5.2).

The server (server.py) sees only `SpeechToText` and `TextToSpeech`. The real
ones are Qwen3-ASR streamed through vLLM and Kokoro on the machine's
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


# Qwen3-ASR's prompt with no context and no forced language, as the official
# SDK (qwen-asr 0.0.6) rendered it through the model's chat_template.json: that
# template always writes the system turn, empty here (vLLM's own transcription
# route omits it). The model answers "language <Name><asr_text><the words>".
ASR_PROMPT = ("<|im_start|>system\n<|im_end|>\n"
              "<|im_start|>user\n<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n"
              "<|im_start|>assistant\n")
ASR_TEXT_TAG = "<asr_text>"
# The SDK's streaming defaults: the first 2 decodes start from nothing; later
# ones start from what was heard, less its last 5 tokens, which may still change.
UNFIXED_CHUNKS = 2
UNFIXED_TOKENS = 5
# A run of one character, or of one pattern of up to 20, repeated more than
# this many times is a decoding loop and kept once (the SDK's threshold).
REPEAT_THRESHOLD = 20
_CHUNK_BYTES = int(CHUNK_SECONDS * STT_RATE) * 2


def _collapse_repeats(text: str, threshold: int = REPEAT_THRESHOLD, max_len: int = 20) -> str:
    """The SDK's repetition repair (qwen_asr/inference/utils.py,
    detect_and_fix_repetitions): a character repeated more than `threshold`
    times is kept once, then the first pattern of up to `max_len` characters
    repeated at least `threshold` times is kept once, and the rest repaired the
    same way."""
    out: list[str] = []
    i = 0
    while i < len(text):
        count = 1
        while i + count < len(text) and text[i + count] == text[i]:
            count += 1
        out.append(text[i] if count > threshold else text[i:i + count])
        i += count
    text = "".join(out)
    n = len(text)
    if n < threshold * 2:
        return text
    for i in range(n - threshold * 2 + 1):
        for k in range(1, max_len + 1):
            if i + k * threshold > n:
                break
            pattern = text[i:i + k]
            if all(text[i + rep * k:i + rep * k + k] == pattern for rep in range(1, threshold)):
                end = i + threshold * k
                while end + k <= n and text[end:end + k] == pattern:
                    end += k
                return text[:i] + pattern + _collapse_repeats(text[end:], threshold, max_len)
    return text


def transcript(raw: str) -> str:
    """The words in Qwen3-ASR's raw answer: what follows <asr_text>, or the whole
    answer when it carries no tag ("language None<asr_text>" is silence)."""
    text = _collapse_repeats(raw.strip())
    if ASR_TEXT_TAG in text:
        text = text.split(ASR_TEXT_TAG, 1)[1]
    return text.strip()


def _float_samples(pcm: bytes):
    import numpy as np

    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class _QwenStream:
    """The SDK's streaming loop (qwen-asr 0.0.6, Qwen3ASRModel.streaming_transcribe
    and finish_streaming_transcribe) on vLLM directly: every CHUNK_SECONDS of new
    audio, all the audio so far is decoded again, prompted with the words already
    settled."""

    def __init__(self, llm, sampling) -> None:
        self._llm = llm
        self._sampling = sampling
        self._tokenizer = llm.get_tokenizer()
        self._pending = b""
        self._heard = b""
        self._raw = ""
        self._text = ""
        self._decodes = 0

    def feed(self, pcm: bytes) -> str:
        self._pending += pcm
        while len(self._pending) >= _CHUNK_BYTES:
            self._heard += self._pending[:_CHUNK_BYTES]
            self._pending = self._pending[_CHUNK_BYTES:]
            self._decode(final=False)
        return self._text

    def finish(self) -> str:
        if self._pending:
            self._heard += self._pending
            self._pending = b""
            self._decode(final=True)
        return self._text

    def _prefix(self, final: bool) -> str:
        if self._decodes < UNFIXED_CHUNKS:
            return ""
        ids = self._tokenizer.encode(self._raw)
        if final:
            return self._tokenizer.decode(ids[:max(1, len(ids) - UNFIXED_TOKENS)])
        drop = UNFIXED_TOKENS
        while True:
            keep = max(0, len(ids) - drop)
            prefix = self._tokenizer.decode(ids[:keep]) if keep > 0 else ""
            # Never cut inside a multi-byte character; at keep 0 the prefix is "".
            if "\ufffd" not in prefix:
                return prefix
            drop += 1

    def _decode(self, *, final: bool) -> None:
        prefix = self._prefix(final)
        request = {"prompt": ASR_PROMPT + prefix, "multi_modal_data": {"audio": [_float_samples(self._heard)]}}
        outputs = self._llm.generate([request], sampling_params=self._sampling, use_tqdm=False)
        self._raw = prefix + outputs[0].outputs[0].text
        self._text = transcript(self._raw)
        self._decodes += 1


class QwenAsr:
    """Qwen3-ASR on vLLM's own model (vllm/model_executor/models/qwen3_asr.py),
    which loads the fetched checkpoint as published. Not through qwen-asr: its
    0.0.6, the last release, pins vllm 0.14.0 and transformers 4.57.6 and does
    not import under transformers 5, which every vLLM that fixes CVE-2026-114053
    needs."""

    def __init__(self) -> None:
        self._llm = None
        self._sampling = None

    def load(self, model_dir: Path) -> None:
        from vllm import LLM, SamplingParams

        self._llm = LLM(model=str(model_dir), gpu_memory_utilization=GPU_MEMORY_UTILIZATION)
        self._sampling = SamplingParams(temperature=0.0, max_tokens=MAX_NEW_TOKENS)

    def begin(self) -> SttStream:
        if self._llm is None:
            raise RuntimeError("the speech-to-text model is not loaded")
        return _QwenStream(self._llm, self._sampling)


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

