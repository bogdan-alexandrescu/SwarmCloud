"""The own-speech engine's turn latency (docs/design/voice-it-own-speech.md
§12.6; swarm_api/voice/speech_relay.py).

One record per turn, handed to the relay's `on_frame` (the telemetry sink):
end of speech (the browser's `utt.end`, sent on the voice detector's end of
the utterance and on the push-to-talk release alike) to the first audio byte
sent to the browser, and its parts on the relay's one monotonic clock. A part
the relay cannot see is None, never 0: the front's first token always (the
front's child streams whole messages), and every part after the point a turn
stopped at.

The browser and the speech server are the in-memory sockets of
test_speech_relay.py and time is its fake clock, moved by the fakes at the
moments a real server or front would take time: no sleep, no network, no cloud
credential.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_speech_relay import ALICE, Dialer, FakeTurns, FakeVm, Harness, run  # noqa: E402

from swarm_api.voice import speech_relay as sr  # noqa: E402
from swarm_api.voice import speech_telemetry as tel  # noqa: E402

#: The keys a turn record may carry: durations, the engine, the turn and the
#: session. A transcript, a reply, audio or anything else is a defect.
RECORD_KEYS = {"t", "kind", "engine", "turn_id", "session_id", "span_ms",
               "recognition_ms", "front_first_token_ms", "front_ms", "synthesis_ms"}


class TimedVm(FakeVm):
    """The fake server, taking `recognise` seconds from `utt.end` to its final
    and `synthesise` seconds from `speak` to its first audio."""

    def __init__(self, clock: Any = None, *, recognise: float = 0.3, synthesise: float = 0.2,
                 speaks: bool = True, heard: str = "what is running") -> None:
        super().__init__(heard=heard)
        self.clock = clock
        self.recognise, self.synthesise, self.speaks = recognise, synthesise, speaks

    async def on_send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            message = json.loads(data)
            if message["t"] == "utt.end":
                self.clock.advance(self.recognise)
            elif message["t"] == "speak":
                if not self.speaks:
                    self.spoken.append(message["text"])
                    return
                self.clock.advance(self.synthesise)
        await super().on_send(data)


class TimedTurns(FakeTurns):
    """The front, taking `think` seconds to answer."""

    def __init__(self, clock: Any = None, *, think: float = 0.8, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock, self.think = clock, think

    def turn(self, session_id: str, text: str, anchor: dict[str, Any] | None) -> sr.FrontLine:
        self.clock.advance(self.think)
        return super().turn(session_id, text, anchor)


def _harness(vm: TimedVm, turns: TimedTurns | None = None, **kw: Any) -> tuple[Harness, list[dict[str, Any]]]:
    turns = turns or TimedTurns()
    h = Harness(dialer=Dialer(vm), turns=turns, **kw)
    vm.clock = h.clock
    turns.clock = h.clock
    frames: list[dict[str, Any]] = []
    h.relay._on_frame = frames.append
    return h, frames


def _turns(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("t") == "timing" and f.get("kind") == "turn"]


def test_a_spoken_turn_records_end_of_speech_to_first_audio_and_its_parts():
    async def go():
        h, frames = _harness(TimedVm())
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: _turns(frames), "a turn record")
        [record] = _turns(frames)
        assert record["engine"] == "own"
        assert record["session_id"] == ALICE.session_id
        assert record["recognition_ms"] == 300.0
        assert record["front_ms"] == 800.0
        assert record["synthesis_ms"] == 200.0
        assert record["span_ms"] == 1300.0
        # The front's child streams whole messages: its first token is not seen.
        assert record["front_first_token_ms"] is None
        await h.close(task)
        assert len(_turns(frames)) == 1, "a turn was recorded twice"
    run(go())


def test_the_record_carries_durations_engine_turn_and_session_only():
    async def go():
        h, frames = _harness(TimedVm(heard="my card number is four four four"))
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: _turns(frames), "a turn record")
        await h.close(task)
        [record] = _turns(frames)
        assert set(record) == RECORD_KEYS
        for key in RECORD_KEYS - {"t", "kind", "engine", "turn_id", "session_id"}:
            assert record[key] is None or isinstance(record[key], float), key
        text = json.dumps(record)
        assert "four" not in text and "On it" not in text
        # The telemetry store takes it as it stands: nothing in it is refused.
        kind, data = tel.clean(record)
        assert kind == tel.KIND_TURN and set(data) == RECORD_KEYS - {"t", "kind"}
    run(go())


def test_a_turn_whose_audio_never_came_is_recorded_not_measured_never_zero():
    async def go():
        h, frames = _harness(TimedVm(speaks=False))
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: h.turns.turns, "the front's turn")
        await h.close(task)
        [record] = _turns(frames)
        assert record["recognition_ms"] == 300.0
        assert record["front_ms"] == 800.0
        assert record["synthesis_ms"] is None and record["span_ms"] is None
    run(go())


def test_a_turn_the_front_never_answered_is_not_measured_after_its_final():
    class Failing(TimedTurns):
        def turn(self, session_id, text, anchor):
            raise RuntimeError("the front is down")

    async def go():
        h, frames = _harness(TimedVm(), Failing())
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: _turns(frames), "a turn record")
        await h.close(task)
        [record] = _turns(frames)
        assert record["recognition_ms"] == 300.0
        assert record["front_ms"] is None and record["synthesis_ms"] is None
        assert record["span_ms"] is None
    run(go())


def test_an_utterance_with_no_words_is_not_a_turn():
    async def go():
        h, frames = _harness(TimedVm(heard="  "))
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: sr.LISTENING in h.browser.statuses(), "listening")
        await h.close(task)
        assert _turns(frames) == []
    run(go())


def test_two_turns_are_two_records_with_their_own_ids():
    async def go():
        h, frames = _harness(TimedVm())
        task = await h.ready()
        await h.utterance(1)
        await h.until(lambda: len(_turns(frames)) == 1, "the first record")
        await h.until(lambda: not h.relay.state.speaking, "the first line spoken")
        await h.utterance(2)
        await h.until(lambda: len(_turns(frames)) == 2, "the second record")
        await h.close(task)
        first, second = _turns(frames)
        assert first["turn_id"] != second["turn_id"]
        assert second["span_ms"] == 1300.0
    run(go())


def test_a_telemetry_sink_that_fails_never_stops_the_turn():
    async def go():
        h, _frames = _harness(TimedVm())

        def broken(frame: dict[str, Any]) -> None:
            raise RuntimeError("store down")

        h.relay._on_frame = broken
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: any(f.get("t") == "say.end" for f in h.browser.frames()), "say.end")
        assert any(isinstance(f, bytes) for f in h.browser.sent), "the reply's audio did not reach the page"
        await h.close(task)
    run(go())

