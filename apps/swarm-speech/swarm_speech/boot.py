"""From a started container to `ready` (docs/design/voice-it-own-speech.md §4.2, §5.2).

The server listens from its first second, so swarm-api's socket gets an answer
while the models are still on their way: each phase is sent to every open
socket as `{"t":"boot","phase":...}` as it begins, and `/v1/health` reports it.

    fetching model <name>   a model in SWARM_SPEECH_MODELS not yet on disk (models.fetch)
    loading models          both models onto the GPU
    warming up              one synthesis and one transcription, below
    ready                   both answered; `{"t":"ready"}` carries the timings
    failed                  with a reason code; swarm-api falls back to text (§4.1)

`updating speech server` comes before this process exists: the launcher
(vm/swarm-speech-launch.sh) pulls a changed image while the port is closed,
and swarm-api, which set the new digest itself (§5.3), shows that status until
the socket answers.

THE WARM-UP is a round trip: Kokoro speaks WARMUP_SENTENCE, the audio is
resampled to 16 kHz and Qwen3-ASR transcribes it, and the words heard must be
the words spoken (WARMUP_MIN_MATCH). One check proves both models produce
something right on this GPU, with no recorded clip to ship or keep in step with
the voice; the design's "bundled 3 s clip" is this sentence, spoken by the
model it then checks. An empty synthesis or a transcript that does not match
fails the boot rather than letting a session start on a broken engine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .engines import STT_RATE, SpeechToText, TextToSpeech, resample_24k_to_16k
from .models import ChecksumMismatch, Download, Models, fetch, hf_download, is_present

log = logging.getLogger("swarm_speech.boot")

WARMUP_SENTENCE = "The quick brown fox jumps over the lazy dog."
# Share of the sentence's words the transcript must contain. Not 1.0: a fair
# transcript may write a word differently ("over" heard as "o'er"); not lower,
# or a model emitting the wrong language would still pass.
WARMUP_MIN_MATCH = 0.75
# The warm-up feeds its audio in one-second pieces, as a speaker's would arrive.
_WARMUP_PIECE = STT_RATE * 2  # bytes per second of 16-bit 16 kHz audio

PHASE_LOADING = "loading models"
PHASE_WARMING = "warming up"
PHASE_READY = "ready"
PHASE_FAILED = "failed"


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def heard_matches(spoken: str, heard: str) -> bool:
    expected = words(spoken)
    got = set(words(heard))
    if not expected:
        return False
    return sum(1 for w in expected if w in got) / len(expected) >= WARMUP_MIN_MATCH


Listener = Callable[[dict], Awaitable[None]]


@dataclass
class Boot:
    """The boot's state, and the listeners (open sockets) told of each change."""

    phase: str = "starting"
    reason: str | None = None
    timings: dict[str, dict[str, int]] = field(
        default_factory=lambda: {"fetch_ms": {}, "load_ms": {}, "warmup_ms": {}})
    listeners: set[Listener] = field(default_factory=set)

    @property
    def ready(self) -> bool:
        return self.phase == PHASE_READY

    def frame(self) -> dict:
        if self.phase == PHASE_READY:
            return {"t": "ready", "timings": self.timings}
        body: dict = {"t": "boot", "phase": self.phase}
        if self.reason:
            body["reason"] = self.reason
        return body

    async def _enter(self, phase: str, reason: str | None = None) -> None:
        self.phase, self.reason = phase, reason
        log.info("boot phase %s%s", phase, f" ({reason})" if reason else "")
        frame = self.frame()
        for listener in list(self.listeners):
            try:
                await listener(frame)
            except Exception:  # noqa: BLE001 -- a closed socket must not stop the boot
                self.listeners.discard(listener)


def _ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


async def run_boot(boot: Boot, *, models: Models | None, config_error: str | None, model_root: Path,
                   stt: SpeechToText, tts: TextToSpeech, download: Download = hf_download) -> None:
    """Fetch, load and warm up; leaves `boot` ready or failed, never raises."""
    if models is None:
        await boot._enter(PHASE_FAILED, "models_config")
        log.error("boot refused SWARM_SPEECH_MODELS: %s", config_error)
        return
    step = "fetch"
    try:
        for spec in (models.stt, models.tts):
            if not is_present(model_root, spec):
                await boot._enter(f"fetching model {spec.name}")
                started = time.monotonic()
                await asyncio.to_thread(fetch, model_root, spec, download)
                boot.timings["fetch_ms"][spec.name] = _ms(started)
        step = "load"
        await boot._enter(PHASE_LOADING)
        started = time.monotonic()
        await asyncio.to_thread(stt.load, model_root / models.stt.dirname)
        boot.timings["load_ms"]["stt"] = _ms(started)
        started = time.monotonic()
        await asyncio.to_thread(tts.load, model_root / models.tts.dirname, repo=models.tts.hf,
                                voices=models.tts.voices)
        boot.timings["load_ms"]["tts"] = _ms(started)
        step = "warmup"
        await boot._enter(PHASE_WARMING)
        reason = await asyncio.to_thread(_warm_up, boot, stt, tts, models.tts.voices[0])
    except ChecksumMismatch as exc:
        log.error("boot failed: %s", exc)
        await boot._enter(PHASE_FAILED, "checksum")
        return
    except Exception as exc:  # noqa: BLE001 -- every failure ends the boot as `failed`
        log.error("boot failed at %s: %s", step, type(exc).__name__)
        await boot._enter(PHASE_FAILED, step)
        return
    if reason:
        await boot._enter(PHASE_FAILED, reason)
        return
    await boot._enter(PHASE_READY)


def _warm_up(boot: Boot, stt: SpeechToText, tts: TextToSpeech, voice: str) -> str | None:
    """None when both models answered right; else the failure's reason code."""
    started = time.monotonic()
    audio = b"".join(tts.synthesize(WARMUP_SENTENCE, voice))
    boot.timings["warmup_ms"]["tts"] = _ms(started)
    if not audio:
        return "warmup_silent"
    pcm = resample_24k_to_16k(audio)
    started = time.monotonic()
    stream = stt.begin()
    for offset in range(0, len(pcm), _WARMUP_PIECE):
        stream.feed(pcm[offset:offset + _WARMUP_PIECE])
    heard = stream.finish()
    boot.timings["warmup_ms"]["stt"] = _ms(started)
    if not heard_matches(WARMUP_SENTENCE, heard):
        log.error("warm-up transcript did not match the spoken sentence")
        return "warmup_mismatch"
    return None
