"""The hosted voice engine's turn latency (docs/design/voice-it-own-speech.md
§12.6; swarm_voice/latency.py, live.py).

From the events the sideband already receives, on swarm-voice's one monotonic
clock: the person's `*.speech_stopped` to the voice's next
`session.output_audio.started`. A turn whose span cannot be taken because one
of the two never came is recorded with `span_ms` None, never 0 and never
estimated. Each record goes to swarm-api beside the turn, through the
planner's `write_latency`, and the sideband never waits for it.

Fake clock, scripted sideband, fake planner: no sleep, no network, no cloud
credential; every id is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import latency as lat  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402
from swarm_voice.grant import SessionGrant  # noqa: E402

RECORD_KEYS = {"engine", "turn_id", "session_id", "span_ms"}
SPOKEN = "my door code is " + secrets.token_hex(4)

STOPPED = {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 4200}
STARTED = {"type": "input_audio_buffer.speech_started", "audio_start_ms": 100}
AUDIO = {"type": live.OUTPUT_AUDIO_STARTED}
AUDIO_DONE = {"type": live.OUTPUT_AUDIO_STOPPED}
HEARD = {"type": "session.input_transcript.done", "item_id": "item_1", "transcript": SPOKEN}
SAID = {"type": "session.output_transcript.done", "item_id": "item_2", "transcript": "Sure."}


class Clock:
    def __init__(self) -> None:
        self.at = 500.0

    def __call__(self) -> float:
        return self.at


def _feed(steps: list[tuple[float, dict]]) -> tuple[list[dict[str, Any]], str]:
    """Each step: seconds to move the clock, then the event."""
    clock = Clock()
    session_id = "vs_" + secrets.token_hex(6)
    turns = lat.TurnLatency(session_id, clock=clock)
    out: list[dict[str, Any]] = []
    for seconds, event in steps:
        clock.at += seconds
        record = turns.note(str(event["type"]), event)
        if record is not None:
            out.append(record)
    return out, session_id


def test_speech_stopped_to_output_audio_started_is_the_span():
    records, session_id = _feed([(0, STARTED), (2.0, STOPPED), (0.65, AUDIO), (3.0, AUDIO_DONE)])
    [record] = records
    assert record["engine"] == "gpt-live"
    assert record["session_id"] == session_id
    assert record["span_ms"] == 650.0
    assert record["turn_id"].startswith("turn_")


def test_the_record_carries_durations_engine_turn_and_session_only():
    records, _ = _feed([(0, STARTED), (0.5, HEARD), (1.0, STOPPED), (0.4, AUDIO), (1.0, SAID)])
    [record] = records
    assert set(record) == RECORD_KEYS
    assert SPOKEN not in json.dumps(record) and "Sure" not in json.dumps(record)


def test_the_person_speaking_again_moves_the_end_of_speech():
    records, _ = _feed([(0, STARTED), (1.0, STOPPED), (0.3, STARTED), (1.0, STOPPED), (0.5, AUDIO)])
    assert [r["span_ms"] for r in records] == [500.0]


def test_a_reply_with_no_audio_started_event_is_not_measured_never_zero():
    # The sideband never delivered session.output_audio.started: the voice's
    # final closes the turn, its span unmeasured.
    records, _ = _feed([(0, STARTED), (1.0, STOPPED), (0.5, HEARD), (2.0, SAID)])
    [record] = records
    assert record["span_ms"] is None


def test_a_reply_with_no_speech_stopped_event_is_not_measured():
    records, _ = _feed([(0.5, HEARD), (0.6, AUDIO), (2.0, SAID)])
    [record] = records
    assert record["span_ms"] is None


def test_the_voice_speaking_unprompted_is_not_a_turn():
    # A planner update spoken while the person is quiet: no end of speech, no
    # words heard since the last reply.
    records, _ = _feed([(0, STARTED), (1.0, STOPPED), (0.4, AUDIO), (2.0, SAID),
                        (10.0, AUDIO), (2.0, SAID)])
    assert [r["span_ms"] for r in records] == [400.0]


def test_a_late_person_final_after_a_measured_reply_adds_no_turn():
    records, _ = _feed([(0, STARTED), (1.0, STOPPED), (0.4, AUDIO), (0.2, HEARD), (2.0, SAID)])
    assert [r["span_ms"] for r in records] == [400.0]


# ---- the live session sends each record beside the turn ---------------------


class _State:
    name = "OPEN"


class ScriptedSocket:
    def __init__(self, events: list[dict], clock: Clock, steps: list[float]) -> None:
        self._events = events
        self._steps = steps
        self._clock = clock
        self.state = _State()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event, seconds in zip(self._events, self._steps):
            self._clock.at += seconds
            yield json.dumps(event)

    async def send(self, raw: str) -> None:
        pass

    async def close(self) -> None:
        pass


class FakePlanner:
    def __init__(self) -> None:
        self.latencies: list[tuple[Any, dict]] = []
        self.traces: list[dict] = []

    async def write_latency(self, grant: Any, record: dict) -> bool:
        self.latencies.append((grant, record))
        return True

    async def write_trace(self, grant: Any, body: dict) -> bool:
        self.traces.append(body)
        return True

    def forget(self, session_id: str) -> None:
        pass


def _grant(session_id: str) -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id=session_id,
                        value=secrets.token_urlsafe(32))


def test_a_live_session_hands_each_record_to_the_planner_off_the_sideband():
    clock = Clock()
    planner = FakePlanner()
    voice_id = "vs_" + secrets.token_hex(16)

    async def main() -> live.LiveSession:
        session = live.LiveSession(voice_id, "live_" + secrets.token_hex(4), credential=None, api=None,
                                   on_event=None, on_closed=lambda s: None, person="owner@example.com",
                                   planner=planner, clock=clock)
        session.grant = _grant(voice_id)
        socket = ScriptedSocket([STARTED, STOPPED, AUDIO, AUDIO_DONE], clock, [0, 1.5, 0.7, 2.0])
        session._socket = socket
        await session._read(socket)
        await asyncio.gather(*list(session._trace_writes), return_exceptions=True)
        return session

    asyncio.run(asyncio.wait_for(main(), 10))
    [(grant, record)] = planner.latencies
    assert grant.session_id == voice_id
    assert record["span_ms"] == 700.0 and record["session_id"] == voice_id
    assert planner.traces == [], "a latency record is not a debug trace"


def test_with_no_grant_nothing_is_sent():
    clock = Clock()
    planner = FakePlanner()

    async def main() -> None:
        session = live.LiveSession("vs_" + secrets.token_hex(6), "live_" + secrets.token_hex(4),
                                   credential=None, api=None, on_event=None, on_closed=lambda s: None,
                                   person="owner@example.com", planner=planner, clock=clock)
        socket = ScriptedSocket([STARTED, STOPPED, AUDIO], clock, [0, 1.0, 0.5])
        await session._read(socket)
        await asyncio.gather(*list(session._trace_writes), return_exceptions=True)

    asyncio.run(asyncio.wait_for(main(), 10))
    assert planner.latencies == []


def test_the_planner_posts_the_record_on_the_grants_trace_route():
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"seq": "x"})

    async def tokens() -> str:
        return "id-" + secrets.token_hex(8)

    session_id = "vs_" + secrets.token_hex(16)
    record = {"engine": "gpt-live", "turn_id": "turn_" + secrets.token_hex(6),
              "session_id": session_id, "span_ms": 812.5}

    async def main() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            planner = pl.Planner(http, api_base="https://swarm-api.test", id_tokens=tokens,
                                 accounts=None, claude=object())
            return await planner.write_latency(_grant(session_id), record)

    assert asyncio.run(main()) is True
    [request] = seen
    assert request.method == "POST"
    assert request.url.path == f"/v1/voice/sessions/{session_id}/trace"
    assert json.loads(request.content) == {"latency": record}
