"""The speech server streams words and speech (docs/design/voice-it-own-speech.md
§3.3, §4.2, §12; apps/swarm-speech/swarm_speech/server.py and boot.py).

Run with a fake speech-to-text and a fake text-to-speech behind the server's
two interfaces (speech_fakes.py): no GPU, no weights, no network, no
credential. What is held here: only swarm-api's ID token opens anything; the
boot reports each phase on the socket and is `ready` only after a warm-up whose
words match; an utterance's frames become partials, a final and a timing
record; speech streams as speak.start, 100 ms binary frames and speak.end, and
a cancel stops it; a metrics frame arrives every interval.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import speech_fakes as fk
from swarm_speech import models as speech_models
from swarm_speech import server
from swarm_speech.boot import WARMUP_SENTENCE
from swarm_speech.telemetry import Sampler

TOKEN = secrets.token_hex(16)


async def admit_only_token(token: str) -> str:
    if token != TOKEN:
        raise PermissionError("not admitted")
    return "swarm-api@example.iam.gserviceaccount.com"


def build(tmp_path: Path, *, stt=None, tts=None, models=None, download=None, config_error=None,
          metrics_seconds: float = 3600.0, proc: Path | None = None):
    stt = stt or fk.FakeStt()
    tts = tts or fk.FakeTts()
    models = models if models is not None or config_error else fk.models()
    sampler = Sampler(disk_path=tmp_path, proc=proc or tmp_path / "no-proc",
                      gpu=lambda: {"util_pct": 12.0, "mem_used_mib": 3000, "mem_total_mib": 23034},
                      vllm=lambda: {"queue": 0.0, "kv_cache_pct": 1.5})
    app = server.create_app(stt=stt, tts=tts, models=models, config_error=config_error,
                            model_root=tmp_path / "models", state_dir=tmp_path / "state",
                            verify_caller=admit_only_token, download=download or fk.FakeDownload(),
                            sampler=sampler, metrics_seconds=metrics_seconds)
    return app, stt, tts


AUTH = {"authorization": f"Bearer {TOKEN}"}


def read_until(ws, done, *, skip=("metrics",)):
    """Every frame up to and including the first JSON frame `done` accepts;
    binary frames as ("audio", length)."""
    seen = []
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            seen.append(("audio", len(message["bytes"])))
            continue
        frame = json.loads(message["text"])
        if frame.get("t") in skip:
            continue
        seen.append(frame)
        if done(frame):
            return seen


def ready(frame) -> bool:
    return frame.get("t") == "ready" or (frame.get("t") == "boot" and frame.get("phase") == "failed")


@contextmanager
def ready_socket(client):
    with client.websocket_connect("/v1/stream", headers=AUTH) as ws:
        seen = read_until(ws, ready)
        assert seen[-1]["t"] == "ready", seen
        yield ws


# ---- the ID-token check (§3.2) ----


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer " + secrets.token_hex(16)},
                                     {"authorization": TOKEN}])
def test_a_socket_without_swarm_apis_token_is_refused_before_it_is_accepted(tmp_path, headers):
    app, *_ = build(tmp_path)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/v1/stream", headers=headers):
                pass
        assert refused.value.code == 1008
        assert app.state.engine.activity.open == 0


def test_health_and_metrics_need_the_token_too(tmp_path):
    app, *_ = build(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 401
        bad = {"authorization": "Bearer " + secrets.token_hex(16)}
        assert client.get("/v1/health", headers=bad).status_code == 403
        assert client.get("/v1/metrics", headers=bad).status_code == 403
        assert client.get("/v1/health", headers=AUTH).status_code == 200


def test_the_caller_check_refuses_to_build_with_no_caller():
    with pytest.raises(RuntimeError, match="SWARM_SPEECH_CALLER"):
        server.google_caller_check(server.AUDIENCE, frozenset())


# ---- the boot (§4.2) ----


def test_the_boot_reports_each_phase_on_the_socket_then_ready(tmp_path):
    download = fk.FakeDownload()
    download.gate = threading.Event()
    app, stt, tts = build(tmp_path, download=download)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream", headers=AUTH) as ws:
            first = ws.receive_json()
            assert first["t"] == "boot"
            download.gate.set()
            seen = [first, *read_until(ws, ready)]
    phases = [f["phase"] for f in seen if f["t"] == "boot" and f["phase"] != "starting"]
    assert phases == ["fetching model qwen3-asr-0.6b", "fetching model kokoro-82m",
                      "loading models", "warming up"]
    final = seen[-1]
    assert final["t"] == "ready"
    assert set(final["timings"]["fetch_ms"]) == {"qwen3-asr-0.6b", "kokoro-82m"}
    assert set(final["timings"]["load_ms"]) == {"stt", "tts"}
    assert set(final["timings"]["warmup_ms"]) == {"stt", "tts"}
    m = fk.models()
    assert stt.loaded.name.startswith("qwen3-asr-0.6b@")
    assert tts.loaded[1:] == ("hexgrad/Kokoro-82M", m.tts.voices)
    # The warm-up spoke its sentence in the first voice, and the transcript was checked.
    assert tts.spoken == [(WARMUP_SENTENCE, "af_heart")]
    assert stt.fed, "the warm-up fed the synthesised audio to speech-to-text"


def test_a_model_already_on_disk_is_not_fetched_again(tmp_path):
    models = fk.models()
    for spec in (models.stt, models.tts):
        speech_models.fetch(tmp_path / "models", spec, fk.FakeDownload())
    download = fk.FakeDownload()
    app, *_ = build(tmp_path, models=models, download=download)
    with TestClient(app) as client:
        with ready_socket(client):
            pass
    assert download.calls == []


@pytest.mark.parametrize("case, reason", [
    ("checksum", "checksum"), ("mismatch", "warmup_mismatch"), ("silent", "warmup_silent"),
    ("load", "load"), ("config", "models_config")])
def test_a_boot_that_cannot_prove_both_models_fails_with_its_reason(tmp_path, case, reason):
    kwargs: dict = {}
    if case == "checksum":
        kwargs["download"] = fk.FakeDownload(corrupt="model.safetensors")
    elif case == "mismatch":
        kwargs["stt"] = fk.FakeStt(transcript="something else entirely was heard")
    elif case == "silent":
        kwargs["tts"] = fk.FakeTts(silent=True)
    elif case == "load":
        stt = fk.FakeStt()
        stt.fail_load = True
        kwargs["stt"] = stt
    else:
        kwargs["config_error"] = "SWARM_SPEECH_MODELS is not set"
    app, *_ = build(tmp_path, **kwargs)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream", headers=AUTH) as ws:
            last = read_until(ws, ready)[-1]
            assert (last["t"], last["phase"], last["reason"]) == ("boot", "failed", reason)
            # Nothing is accepted while the engine is not ready.
            ws.send_json({"t": "utt.start", "seq": 1})
            assert read_until(ws, lambda f: f["t"] == "error")[-1] == {
                "t": "error", "code": "not_ready", "phase": "failed"}
        health = client.get("/v1/health", headers=AUTH).json()
    assert (health["ready"], health["reason"]) == (False, reason)
    if case == "checksum":
        assert not list((tmp_path / "models").glob("qwen3-asr-0.6b@*")), "a refused model leaves no directory"


# ---- speech to text over the socket (§3.3 steps 2-4) ----


def test_an_utterance_streams_partials_then_a_final_and_a_timing_record(tmp_path):
    stt = fk.FakeStt()  # the warm-up's sentence until ready, then this utterance's words
    app, *_ = build(tmp_path, stt=stt)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            stt.transcript = "merge the pull request now"
            ws.send_json({"t": "utt.start", "seq": 7})
            frame = b"\x00\x01" * 320  # 20 ms of 16 kHz 16-bit PCM: 640 bytes
            partials = []
            for n in range(25):
                ws.send_bytes(frame)
                if (n + 1) % 5 == 0:  # every 100 ms reveals a word
                    partials.append(read_until(ws, lambda f: f["t"] == "partial")[-1])
            ws.send_json({"t": "utt.end", "seq": 7})
            seen = read_until(ws, lambda f: f["t"] == "timing")
    assert [p["text"] for p in partials] == ["merge", "merge the", "merge the pull",
                                             "merge the pull request", "merge the pull request now"]
    assert all(p["seq"] == 7 for p in partials)
    final = next(f for f in seen if f["t"] == "final")
    assert final == {"t": "final", "seq": 7, "text": "merge the pull request now"}
    timing = seen[-1]
    assert timing["kind"] == "stt" and timing["seq"] == 7 and timing["ok"] is True
    assert timing["audio_s"] == pytest.approx(25 * 0.02)
    assert timing["first_partial_ms"] is not None and timing["final_ms"] >= 0
    assert app.state.engine.counters.utterances == 1


def test_frames_outside_an_utterance_or_misshapen_are_refused_and_the_socket_stays(tmp_path):
    app, *_ = build(tmp_path)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            ws.send_bytes(b"\x00\x00" * 320)
            ws.send_bytes(b"\x00\x00" * 320)  # warned once, not once per frame
            ws.send_json({"t": "utt.start", "seq": 1})
            ws.send_bytes(b"\x00" * 641)  # half a sample
            ws.send_bytes(b"\x00" * (server.MAX_FRAME_BYTES + 2))
            ws.send_json({"t": "utt.end", "seq": 2})
            ws.send_json({"t": "utt.start", "seq": "1"})
            ws.send_text("{not json")
            ws.send_json({"t": "shell", "cmd": "ls"})
            ws.send_json({"t": "ping"})
            seen = read_until(ws, lambda f: f["t"] == "pong")
    codes = [f.get("code") for f in seen if f["t"] == "error"]
    assert codes == ["audio_outside_utterance", "bad_frame", "bad_frame", "no_such_utterance",
                     "bad_seq", "bad_json", "unknown_message"]


def test_an_utterance_past_a_minute_is_cut_and_finalised(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_UTTERANCE_SECONDS", 1)
    app, *_ = build(tmp_path)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            ws.send_json({"t": "utt.start", "seq": 3})
            for _ in range(60):  # 1.2 s of audio
                ws.send_bytes(b"\x00\x01" * 320)
            seen = read_until(ws, lambda f: f["t"] == "timing")
            ws.send_json({"t": "utt.end", "seq": 3})
            ws.send_json({"t": "ping"})
            after = read_until(ws, lambda f: f["t"] == "pong")
    final = next(f for f in seen if f["t"] == "final")
    assert final["cut"] is True and final["seq"] == 3
    assert seen[-1]["audio_s"] == pytest.approx(1.0)
    # The cut utterance's own end is not an error, nor is it finalised twice.
    assert [f["t"] for f in after] == ["pong"]


def test_a_decoder_failure_is_a_final_with_an_error_not_a_dead_socket(tmp_path):
    stt = fk.FakeStt()
    app, *_ = build(tmp_path, stt=stt)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            stt.fail_feed = True
            ws.send_json({"t": "utt.start", "seq": 4})
            ws.send_bytes(b"\x00\x01" * 320)
            ws.send_json({"t": "utt.end", "seq": 4})
            seen = read_until(ws, lambda f: f["t"] == "timing")
            ws.send_json({"t": "ping"})
            assert read_until(ws, lambda f: f["t"] == "pong")
    final = next(f for f in seen if f["t"] == "final")
    assert final == {"t": "final", "seq": 4, "text": "", "error": "stt_failed"}
    assert seen[-1]["ok"] is False
    assert app.state.engine.counters.stt_errors == 1


# ---- text to speech over the socket (§3.3 steps 5-6) ----


def test_speak_streams_audio_in_100_ms_frames_between_start_and_end(tmp_path):
    app, _, tts = build(tmp_path)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            ws.send_json({"t": "speak", "id": "r1", "text": "Will merge pull request 12. Go?",
                          "voice": "bm_george"})
            seen = read_until(ws, lambda f: f["t"] == "timing")
    assert seen[0] == {"t": "speak.start", "id": "r1", "rate": 24000}
    audio = [n for kind, n in (s for s in seen if isinstance(s, tuple))]
    assert sum(audio) == 2 * fk.FakeTts.SENTENCE_BYTES
    assert max(audio) == server.OUT_CHUNK_BYTES == 4800
    end = next(f for f in seen if isinstance(f, dict) and f["t"] == "speak.end")
    assert end == {"t": "speak.end", "id": "r1", "cancelled": False}
    timing = seen[-1]
    assert timing["kind"] == "tts" and timing["audio_s"] == pytest.approx(1.0)
    assert timing["first_audio_ms"] is not None
    assert tts.spoken[-1] == ("Will merge pull request 12. Go?", "bm_george")


@pytest.mark.parametrize("frame, code", [
    ({"t": "speak", "id": "r1", "text": "hi", "voice": "/models/evil.pt"}, "unknown_voice"),
    ({"t": "speak", "id": "r1", "text": "hi", "voice": "zz_nobody"}, "unknown_voice"),
    ({"t": "speak", "id": "r1", "text": "", "voice": "af_heart"}, "bad_text"),
    ({"t": "speak", "id": "r1", "text": "x" * (server.MAX_SPEAK_CHARS + 1), "voice": "af_heart"}, "bad_text"),
    ({"t": "speak", "id": "../r1", "text": "hi", "voice": "af_heart"}, "bad_id")])
def test_speak_takes_a_loaded_voice_by_name_and_bounded_text_only(tmp_path, frame, code):
    app, _, tts = build(tmp_path)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            ws.send_json(frame)
            seen = read_until(ws, lambda f: f["t"] == "error")
    assert seen[-1]["code"] == code
    assert len(tts.spoken) == 1, "only the warm-up was spoken"


def test_cancel_stops_speech_mid_reply_and_says_so(tmp_path):
    tts = fk.FakeTts()
    app, *_ = build(tmp_path, tts=tts)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            tts.gate = threading.Event()
            ws.send_json({"t": "speak", "id": "r2", "text": "First sentence. Second. Third.",
                          "voice": "af_heart"})
            ws.send_json({"t": "speak", "id": "r3", "text": "Queued behind it.", "voice": "af_heart"})
            read_until(ws, lambda f: f["t"] == "speak.start")
            ws.send_json({"t": "cancel"})  # barge-in: everything stops
            tts.gate.set()
            seen = read_until(ws, lambda f: f["t"] == "speak.end" and f["id"] == "r3")
    ends = [f for f in seen if isinstance(f, dict) and f["t"] == "speak.end"]
    assert ends == [{"t": "speak.end", "id": "r2", "cancelled": True},
                    {"t": "speak.end", "id": "r3", "cancelled": True}]
    audio = sum(n for s in seen if isinstance(s, tuple) for n in [s[1]])
    assert audio < 3 * fk.FakeTts.SENTENCE_BYTES


# ---- metrics frames (§12.1) ----


def test_a_metrics_frame_arrives_every_interval_with_the_machines_readings(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal:       16000000 kB\nMemAvailable:    8000000 kB\n")
    app, *_ = build(tmp_path, metrics_seconds=0.05, proc=proc)
    with TestClient(app) as client:
        with ready_socket(client) as ws:
            frames = [read_until(ws, lambda f: f["t"] == "metrics", skip=())[-1] for _ in range(2)]
    frame = frames[-1]
    assert frame["gpu"] == {"util_pct": 12.0, "mem_used_mib": 3000, "mem_total_mib": 23034}
    assert frame["vllm"] == {"queue": 0.0, "kv_cache_pct": 1.5}
    assert frame["memory"] == {"used_mib": 7812, "total_mib": 15625}
    assert frame["disk"]["total_gb"] > 0
    assert frame["open_sockets"] == 1 and frame["phase"] == "ready"
    assert set(frame["counters"]) >= {"utterances", "stt_errors", "syntheses", "tts_cancelled"}


def test_the_activity_file_counts_open_sockets_for_the_watchdog(tmp_path):
    app, *_ = build(tmp_path)
    state = tmp_path / "state" / "activity.json"
    with TestClient(app) as client:
        with ready_socket(client):
            assert json.loads(state.read_text())["open"] == 1
        # The server's side of the close lands a moment after the client's.
        deadline = time.monotonic() + 5
        while json.loads(state.read_text())["open"] and time.monotonic() < deadline:
            time.sleep(0.01)
    assert json.loads(state.read_text())["open"] == 0
