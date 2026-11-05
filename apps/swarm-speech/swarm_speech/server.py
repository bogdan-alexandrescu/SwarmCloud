"""The speech server's routes and its socket (docs/design/voice-it-own-speech.md
§3.2, §3.3, §4.2, §12.1).

One WebSocket per swarm-api socket, `GET /v1/stream`, and two reads,
`/v1/health` and `/v1/metrics`. Every route needs a Google ID token for the
audience `swarm-speech` whose email is swarm-api's service account (passed in
as SWARM_SPEECH_CALLER from the instance's metadata): tenant jobs share the
subnet, so the address a socket comes from proves nothing (§3.2). A socket
without one is refused before it is accepted.

swarm-api -> server, JSON text frames:

    {"t":"utt.start","seq":n}           an utterance begins; binary frames follow
    <binary>                            16 kHz mono 16-bit LE PCM (any whole number
                                        of samples, at most MAX_FRAME_BYTES)
    {"t":"utt.end","seq":n}             the utterance ended; its final follows
    {"t":"speak","id":s,"text":s,"voice":s}   say this; queued behind any other
    {"t":"cancel"} / {"t":"cancel","id":s}    stop all speech (barge-in) / that one
    {"t":"ping"}

server -> swarm-api:

    {"t":"boot","phase":...,"reason"?}  until ready (boot.py); then {"t":"ready","timings"}
    {"t":"partial","seq":n,"text":s}    the words so far, when they change
    {"t":"final","seq":n,"text":s,"cut"?,"error"?}
    {"t":"speak.start","id":s,"rate":24000}, <binary 24 kHz PCM>..., {"t":"speak.end","id":s,"cancelled":b}
    {"t":"timing","kind":"stt"|"tts",...}   one per utterance and per synthesis (§12.2)
    {"t":"metrics",...}                 every METRICS_SECONDS (telemetry.py)
    {"t":"pong"}
    {"t":"error","code":s,...}          a frame refused; the socket stays open

Binary frames are unambiguous in each direction: in, they belong to the open
utterance; out, to the synthesis between its speak.start and speak.end, and one
synthesis runs at a time per socket. Nothing the socket carries is logged:
not audio, not words, not text to speak. Only codes and counts reach stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Header, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .activity import REFRESH_SECONDS, Activity
from .boot import Boot, run_boot
from .engines import STT_RATE, TTS_RATE, SpeechToText, TextToSpeech
from .models import Download, Models, hf_download
from .telemetry import Counters, Sampler

log = logging.getLogger("swarm_speech.server")

AUDIENCE = "swarm-speech"
# One second of 16 kHz 16-bit audio. The browser sends 20 ms (640 bytes);
# swarm-api may batch, but never a second's worth in one frame.
MAX_FRAME_BYTES = STT_RATE * 2
# Qwen3-ASR's streaming path re-decodes all of an utterance's audio at each
# chunk, so its cost grows with the square of the length. Past a minute the
# utterance is finalised (`cut`) and the rest of it dropped until utt.end:
# spoken commands are sentences, not monologues.
MAX_UTTERANCE_SECONDS = 60
MAX_SPEAK_CHARS = 4000
# One session at a time (§4.6), and a rotation overlaps two of its sockets
# (§3.3); four leaves room for a reconnect racing a close.
MAX_SOCKETS = 4
METRICS_SECONDS = 5.0
# Speech goes out in 100 ms frames, so swarm-api forwards it as it comes.
OUT_CHUNK_BYTES = TTS_RATE * 2 // 10
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CallerCheck = Callable[[str], Awaitable[str]]


def google_caller_check(audience: str, callers: frozenset[str]) -> CallerCheck:
    """An ID-token check pinned to `audience` that admits only `callers`."""
    if not audience:
        raise RuntimeError("the speech server's token audience is empty")
    if not callers:
        raise RuntimeError("SWARM_SPEECH_CALLER is empty: nothing could call the speech server")
    request_holder: list[object] = []

    def verify(token: str) -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        if not request_holder:
            request_holder.append(google_requests.Request())
        try:
            claims = id_token.verify_oauth2_token(token, request_holder[0], audience=audience)
        except ValueError:
            # google-auth's message may echo token content: never propagated.
            raise PermissionError("id token verification failed") from None
        email = claims.get("email")
        if not email or claims.get("email_verified") is False or email not in callers:
            raise PermissionError("the caller is not admitted")
        return str(email)

    async def check(token: str) -> str:
        return await asyncio.to_thread(verify, token)

    return check


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


@dataclass
class Engine:
    """What every socket shares: the boot, the two models, their locks, the counters."""

    stt: SpeechToText
    tts: TextToSpeech
    models: Models | None
    activity: Activity
    sampler: Sampler
    boot: Boot = field(default_factory=Boot)
    counters: Counters = field(default_factory=Counters)
    # One GPU: one call into each model at a time, across every socket.
    stt_lock: threading.Lock = field(default_factory=threading.Lock)
    tts_lock: threading.Lock = field(default_factory=threading.Lock)
    sockets: set["StreamSocket"] = field(default_factory=set)
    last_metrics: dict | None = None

    @property
    def voices(self) -> tuple[str, ...]:
        return self.models.tts.voices if self.models else ()


@dataclass
class _Utterance:
    seq: int
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    received_bytes: int = 0
    cut: bool = False
    first_frame_at: float | None = None
    end_at: float | None = None


@dataclass
class _Speech:
    id: str
    text: str
    voice: str
    cancel: threading.Event = field(default_factory=threading.Event)


class StreamSocket:
    def __init__(self, ws: WebSocket, engine: Engine) -> None:
        self.ws = ws
        self.engine = engine
        self._send_lock = asyncio.Lock()
        self._utterance: _Utterance | None = None
        self._warned_outside = False
        self._speech_queue: asyncio.Queue[_Speech] = asyncio.Queue()
        self._pending: list[_Speech] = []
        self._current: _Speech | None = None
        self._speaker: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- sending ----

    async def send(self, frame: dict) -> None:
        async with self._send_lock:
            if self.ws.application_state == WebSocketState.CONNECTED:
                await self.ws.send_text(json.dumps(frame, separators=(",", ":")))

    async def send_audio(self, pcm: bytes) -> None:
        async with self._send_lock:
            if self.ws.application_state == WebSocketState.CONNECTED:
                await self.ws.send_bytes(pcm)

    async def error(self, code: str, **extra: object) -> None:
        self.engine.counters.protocol_errors += 1
        await self.send({"t": "error", "code": code, **extra})

    # ---- the loop ----

    async def run(self) -> None:
        self._speaker = asyncio.create_task(self._speak_loop())
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await self._audio(message["bytes"])
                elif message.get("text") is not None:
                    await self._text(message["text"])
        except WebSocketDisconnect:
            return
        finally:
            await self._close()

    async def _close(self) -> None:
        for speech in [*self._pending, *([self._current] if self._current else [])]:
            speech.cancel.set()
        tasks = [t for t in (self._speaker, *self._tasks) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _text(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            await self.error("bad_json")
            return
        kind = frame.get("t") if isinstance(frame, dict) else None
        if kind == "ping":
            await self.send({"t": "pong"})
        elif kind in ("utt.start", "utt.end", "speak") and not self.engine.boot.ready:
            await self.error("not_ready", phase=self.engine.boot.phase)
        elif kind == "utt.start":
            await self._utt_start(frame)
        elif kind == "utt.end":
            await self._utt_end(frame)
        elif kind == "speak":
            await self._speak(frame)
        elif kind == "cancel":
            await self._cancel(frame)
        else:
            await self.error("unknown_message")

    # ---- speech to text ----

    @staticmethod
    def _seq(frame: dict) -> int | None:
        seq = frame.get("seq")
        return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None

    async def _utt_start(self, frame: dict) -> None:
        seq = self._seq(frame)
        if seq is None:
            await self.error("bad_seq")
            return
        if self._utterance is not None:
            # A start before the last one's end: the last one is finished as it stands.
            self._end_utterance(self._utterance)
        utterance = _Utterance(seq=seq, queue=asyncio.Queue())
        utterance.task = self._spawn(self._transcribe(utterance))
        self._utterance = utterance
        self._warned_outside = False

    async def _utt_end(self, frame: dict) -> None:
        seq = self._seq(frame)
        current = self._utterance
        if current is None or seq != current.seq:
            await self.error("no_such_utterance", seq=seq)
            return
        self._end_utterance(current)

    def _end_utterance(self, utterance: _Utterance) -> None:
        if utterance.end_at is None:
            utterance.end_at = time.monotonic()
            utterance.queue.put_nowait(None)
        if self._utterance is utterance:
            self._utterance = None

    async def _audio(self, pcm: bytes) -> None:
        utterance = self._utterance
        if utterance is None:
            if not self._warned_outside:
                self._warned_outside = True
                await self.error("audio_outside_utterance")
            return
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_FRAME_BYTES:
            await self.error("bad_frame", seq=utterance.seq)
            return
        if utterance.cut:
            return
        if utterance.first_frame_at is None:
            utterance.first_frame_at = time.monotonic()
        utterance.received_bytes += len(pcm)
        utterance.queue.put_nowait(pcm)
        if utterance.received_bytes >= MAX_UTTERANCE_SECONDS * STT_RATE * 2:
            utterance.cut = True
            utterance.end_at = time.monotonic()
            utterance.queue.put_nowait(None)

    async def _transcribe(self, utterance: _Utterance) -> None:
        engine = self.engine
        counters = engine.counters
        processing = 0.0
        first_partial_ms: int | None = None
        last_partial = ""
        error: str | None = None
        text = ""

        def locked(call, *args):
            with engine.stt_lock:
                return call(*args)

        try:
            stream = await asyncio.to_thread(locked, engine.stt.begin)
            done = False
            while not done:
                pieces = [await utterance.queue.get()]
                while not utterance.queue.empty():
                    pieces.append(utterance.queue.get_nowait())
                if None in pieces:
                    done = True
                pcm = b"".join(p for p in pieces if p is not None)
                if not pcm:
                    continue
                started = time.monotonic()
                partial = await asyncio.to_thread(locked, stream.feed, pcm)
                processing += time.monotonic() - started
                if partial and partial != last_partial:
                    last_partial = partial
                    if first_partial_ms is None and utterance.first_frame_at is not None:
                        first_partial_ms = int((time.monotonic() - utterance.first_frame_at) * 1000)
                    if not done:
                        await self.send({"t": "partial", "seq": utterance.seq, "text": partial})
            started = time.monotonic()
            text = await asyncio.to_thread(locked, stream.finish)
            processing += time.monotonic() - started
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one utterance's failure is reported, not fatal
            log.error("speech-to-text failed: %s", type(exc).__name__)
            error = "stt_failed"
        text = (text or "").strip()
        audio_s = utterance.received_bytes / (STT_RATE * 2)
        counters.utterances += 1
        counters.stt_audio_seconds += audio_s
        if error or not text:
            counters.stt_errors += 1
        final: dict = {"t": "final", "seq": utterance.seq, "text": text}
        if utterance.cut:
            final["cut"] = True
        if error:
            final["error"] = error
        await self.send(final)
        end_at = utterance.end_at or time.monotonic()
        await self.send({
            "t": "timing", "kind": "stt", "seq": utterance.seq,
            "final_ms": int((time.monotonic() - end_at) * 1000),
            "first_partial_ms": first_partial_ms,
            "audio_s": round(audio_s, 3), "processing_s": round(processing, 3),
            "rtf": round(processing / audio_s, 3) if audio_s else None,
            "ok": error is None and bool(text), "cut": utterance.cut})

    # ---- text to speech ----

    async def _speak(self, frame: dict) -> None:
        speech_id, text, voice = frame.get("id"), frame.get("text"), frame.get("voice")
        if not isinstance(speech_id, str) or not _ID.match(speech_id):
            await self.error("bad_id")
            return
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SPEAK_CHARS:
            await self.error("bad_text", id=speech_id)
            return
        # A voice by name, from the loaded list only (invariant 10's rule: never a path or a file).
        if not isinstance(voice, str) or voice not in self.engine.voices:
            await self.error("unknown_voice", id=speech_id)
            return
        speech = _Speech(id=speech_id, text=text, voice=voice)
        self._pending.append(speech)
        self._speech_queue.put_nowait(speech)

    async def _cancel(self, frame: dict) -> None:
        target = frame.get("id")
        if target is not None and not (isinstance(target, str) and _ID.match(target)):
            await self.error("bad_id")
            return
        for speech in [*self._pending, *([self._current] if self._current else [])]:
            if target is None or speech.id == target:
                speech.cancel.set()

    async def _speak_loop(self) -> None:
        while True:
            speech = await self._speech_queue.get()
            self._pending.remove(speech)
            self._current = speech
            try:
                await self._synthesize(speech)
            finally:
                self._current = None

    async def _synthesize(self, speech: _Speech) -> None:
        engine = self.engine
        counters = engine.counters
        if speech.cancel.is_set():
            counters.tts_cancelled += 1
            await self.send({"t": "speak.end", "id": speech.id, "cancelled": True})
            return
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue = asyncio.Queue()
        done = object()
        failed: list[str] = []

        def produce() -> None:
            # In a thread: Kokoro's generator, one sentence at a time, checked
            # for a cancel between sentences.
            try:
                with engine.tts_lock:
                    for pcm in engine.tts.synthesize(speech.text, speech.voice):
                        if speech.cancel.is_set():
                            break
                        loop.call_soon_threadsafe(chunks.put_nowait, pcm)
            except Exception as exc:  # noqa: BLE001 -- reported on the socket as speak.end's error
                log.error("text-to-speech failed: %s", type(exc).__name__)
                failed.append("tts_failed")
            finally:
                loop.call_soon_threadsafe(chunks.put_nowait, done)

        received = time.monotonic()
        await self.send({"t": "speak.start", "id": speech.id, "rate": TTS_RATE})
        worker = loop.run_in_executor(None, produce)
        first_audio_ms: int | None = None
        audio_bytes = 0
        try:
            while True:
                pcm = await chunks.get()
                if pcm is done:
                    break
                if speech.cancel.is_set():
                    continue
                for offset in range(0, len(pcm), OUT_CHUNK_BYTES):
                    if speech.cancel.is_set():
                        break
                    piece = pcm[offset:offset + OUT_CHUNK_BYTES]
                    if first_audio_ms is None:
                        first_audio_ms = int((time.monotonic() - received) * 1000)
                    audio_bytes += len(piece)
                    await self.send_audio(piece)
        except asyncio.CancelledError:
            # The socket closed: the thread stops at its next sentence.
            speech.cancel.set()
            raise
        await worker
        synth_s = time.monotonic() - received
        cancelled = speech.cancel.is_set()
        audio_s = audio_bytes / (TTS_RATE * 2)
        counters.syntheses += 1
        counters.tts_audio_seconds += audio_s
        if cancelled:
            counters.tts_cancelled += 1
        end: dict = {"t": "speak.end", "id": speech.id, "cancelled": cancelled}
        if failed:
            counters.tts_errors += 1
            end["error"] = failed[0]
        await self.send(end)
        await self.send({
            "t": "timing", "kind": "tts", "id": speech.id, "first_audio_ms": first_audio_ms,
            "audio_s": round(audio_s, 3), "synth_s": round(synth_s, 3),
            "rtf": round(synth_s / audio_s, 3) if audio_s else None,
            "cancelled": cancelled, "ok": not failed})

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


async def _metrics_loop(engine: Engine, every: float) -> None:
    ticks_per_refresh = max(1, int(REFRESH_SECONDS / every))
    tick = 0
    while True:
        await asyncio.sleep(every)
        tick += 1
        if tick % ticks_per_refresh == 0:
            engine.activity.refresh()
        sample = await asyncio.to_thread(engine.sampler.sample)
        frame = {"t": "metrics", "at": round(time.time(), 3), **sample,
                 "counters": engine.counters.as_dict(), "open_sockets": len(engine.sockets),
                 "phase": engine.boot.phase}
        engine.last_metrics = frame
        for sock in list(engine.sockets):
            try:
                await sock.send(frame)
            except Exception:  # noqa: BLE001 -- a socket closing mid-send; its own loop ends it
                continue


def create_app(*, stt: SpeechToText, tts: TextToSpeech, models: Models | None, config_error: str | None,
               model_root: Path, state_dir: Path, verify_caller: CallerCheck,
               download: Download = hf_download, sampler: Sampler | None = None,
               metrics_seconds: float = METRICS_SECONDS) -> FastAPI:
    engine = Engine(stt=stt, tts=tts, models=models, activity=Activity(state_dir),
                    sampler=sampler or Sampler(disk_path=model_root))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        model_root.mkdir(parents=True, exist_ok=True)
        boot = asyncio.create_task(run_boot(engine.boot, models=models, config_error=config_error,
                                            model_root=model_root, stt=stt, tts=tts, download=download))
        metrics = asyncio.create_task(_metrics_loop(engine, metrics_seconds))
        try:
            yield
        finally:
            for task in (boot, metrics):
                task.cancel()
            await asyncio.gather(boot, metrics, return_exceptions=True)

    app = FastAPI(title="swarm-speech", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine

    async def admit(authorization: str | None) -> None:
        token = _bearer(authorization)
        if token is None:
            raise HTTPException(status_code=401, detail="a Google ID token is required")
        try:
            await verify_caller(token)
        except PermissionError:
            raise HTTPException(status_code=403, detail="the caller is not admitted") from None

    @app.get("/v1/health")
    async def health(authorization: str | None = Header(default=None)) -> dict:
        await admit(authorization)
        boot = engine.boot
        return {"phase": boot.phase, "reason": boot.reason, "ready": boot.ready,
                "models": {"stt": models.stt.name, "tts": models.tts.name} if models else None,
                "voices": list(engine.voices), "timings": boot.timings,
                "open_sockets": len(engine.sockets)}

    @app.get("/v1/metrics")
    async def metrics(authorization: str | None = Header(default=None)) -> dict:
        await admit(authorization)
        return {"metrics": engine.last_metrics, "counters": engine.counters.as_dict()}

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        token = _bearer(ws.headers.get("authorization"))
        try:
            if token is None:
                raise PermissionError("no token")
            await verify_caller(token)
        except PermissionError:
            log.warning("refused a speech socket without an admitted ID token")
            await ws.close(code=1008)
            return
        if len(engine.sockets) >= MAX_SOCKETS:
            await ws.close(code=1013)
            return
        await ws.accept()
        sock = StreamSocket(ws, engine)
        engine.sockets.add(sock)
        engine.activity.opened()
        frame = engine.boot.frame()
        engine.boot.listeners.add(sock.send)
        try:
            await sock.send(frame)
            await sock.run()
        finally:
            engine.boot.listeners.discard(sock.send)
            engine.sockets.discard(sock)
            engine.activity.closed()

    return app
