"""A GPT-Live planner turn hears the person when the sideband sends transcript
DELTAS only (live.py, "THE SIDEBAND SENDS ONLY DELTAS").

THE DEFECT (dev, 10 November 2026). The voice answered the person, but every
planner turn started with an empty transcript and drew nothing. The session's
event types showed `session.input_transcript.delta` and
`session.output_transcript.delta` and never a final, and the planner's window
was filled from finals only. This holds:

* deltas of both sides, grouped into one line per item, reach the planner's
  transcript in order, the person's request included while it is still
  mid-segment when the delegation arrives;
* a delta segment followed by its final is one line, the final's text;
* the model's own commentary never becomes a line of the person's;
* a turn with no text at all still asks the person to say it again;
* no delta's text is logged.

Offline: a LiveSession reads a scripted socket; the planner is a fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import live  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402

PERSON = "session.input_transcript.delta"
VOICE = "session.output_transcript.delta"


class _State:
    name = "OPEN"


class ScriptedSocket:
    """The sideband: yields the scripted events, records what is sent."""

    def __init__(self, events: list[dict]) -> None:
        self._events = [json.dumps(e) for e in events]
        self.sent: list[dict] = []
        self.state = _State()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for raw in self._events:
            yield raw

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        pass


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def delegate(self, **kwargs):
        # A copy: what the planner was handed at the turn's start.
        self.calls.append({**kwargs, "transcript": list(kwargs["transcript"])})
        return pl.TurnResult(kwargs["delegation_id"], "It's on screen.")

    def forget(self, session_id: str) -> None:
        pass


def _delegation() -> tuple[str, dict]:
    delegation_id = "item_" + secrets.token_hex(6)
    return delegation_id, {"type": live.DELEGATION_CREATED,
                           "delegation": {"id": delegation_id, "type": "delegation"}}


def _deltas(kind: str, words: str, **ids) -> list[dict]:
    """`words` as the sideband sends it: a delta a word, spaces kept."""
    parts = words.split(" ")
    pieces = [part + " " for part in parts[:-1]] + [parts[-1]]
    return [{"type": kind, "delta": piece, **ids} for piece in pieces]


def _run(events: list[dict]) -> tuple[live.LiveSession, ScriptedSocket, FakePlanner]:
    planner = FakePlanner()
    socket = ScriptedSocket(events)

    async def main() -> live.LiveSession:
        session = live.LiveSession("vs_" + secrets.token_hex(4), "live_" + secrets.token_hex(4),
                                   credential=None, api=None, on_event=None,
                                   on_closed=lambda s: None, person="owner@example.com",
                                   planner=planner)
        session._socket = socket
        await session._read(socket)
        for _ in range(200):
            if not session._turns:
                break
            await asyncio.sleep(0.01)
        return session

    return asyncio.run(asyncio.wait_for(main(), 10)), socket, planner


def _said(socket: ScriptedSocket) -> list[str]:
    return [e["content"] for e in socket.sent if e.get("type") == live.COMMENTARY_APPEND]


def test_deltas_of_both_sides_give_the_planner_the_persons_request():
    delegation_id, delegated = _delegation()
    _, socket, planner = _run([
        {"type": "session.started"},
        {"type": "session.input_audio.append"},
        *_deltas(PERSON, "Hello there.", item_id="item_a"),
        *_deltas(VOICE, "Hi, what are we building?", response_id="resp_a"),
        {"type": "session.output_audio.delta"},
        *_deltas(PERSON, "Draw me a website for a bakery.", item_id="item_b"),
        # MUTATION: build the window from finals only and this turn starts empty,
        # as the owner saw: the request is still mid-segment when it arrives.
        delegated,
    ])
    assert len(planner.calls) == 1
    assert planner.calls[0]["delegation_id"] == delegation_id
    assert planner.calls[0]["transcript"] == [
        ("person", "Hello there."),
        ("voice", "Hi, what are we building?"),
        ("person", "Draw me a website for a bakery."),
    ]
    assert _said(socket) == ["It's on screen."]


def test_deltas_without_ids_are_split_by_a_change_of_speaker():
    _, delegated = _delegation()
    _, _, planner = _run([
        *_deltas(PERSON, "Help me plan"),
        *_deltas(VOICE, "Sure."),
        *_deltas(PERSON, "a shop page."),
        {"type": "session.input_transcript.delta", "text": " Thanks"},
        delegated,
    ])
    assert planner.calls[0]["transcript"] == [
        ("person", "Help me plan"),
        ("voice", "Sure."),
        ("person", "a shop page. Thanks"),
    ]


def test_interleaved_items_keep_one_line_each_in_the_order_they_began():
    _, delegated = _delegation()
    _, _, planner = _run([
        {"type": PERSON, "delta": "Make it ", "item_id": "item_a"},
        {"type": VOICE, "delta": "Okay", "response_id": "resp_a"},
        {"type": PERSON, "delta": "blue.", "item_id": "item_a"},
        {"type": VOICE, "delta": ", blue it is.", "response_id": "resp_a"},
        {"type": "response.output_audio_transcript.delta", "delta": "Drawing.", "response_id": "resp_b"},
        delegated,
    ])
    assert planner.calls[0]["transcript"] == [
        ("person", "Make it blue."),
        ("voice", "Okay, blue it is."),
        ("voice", "Drawing."),
    ]


def test_a_delta_segment_followed_by_its_final_is_counted_once():
    _, delegated = _delegation()
    session, _, planner = _run([
        *_deltas(PERSON, "Draw a home page", item_id="item_a"),
        # MUTATION: append the final as a line of its own and the request is heard twice.
        {"type": "session.input_transcript.done", "item_id": "item_a",
         "transcript": "Draw a home page."},
        *_deltas(VOICE, "Will do", response_id="resp_a"),
        {"type": "session.output_transcript.done", "transcript": "Will do."},
        # A late delta of a finished item adds nothing.
        {"type": PERSON, "delta": " again", "item_id": "item_a"},
        delegated,
    ])
    assert planner.calls[0]["transcript"] == [
        ("person", "Draw a home page."),
        ("voice", "Will do."),
    ]
    assert session.transcript == planner.calls[0]["transcript"]


def test_the_models_commentary_is_not_a_line_of_the_persons():
    words = "Let me look that up " + secrets.token_hex(3)
    _, delegated = _delegation()
    _, _, planner = _run([
        *_deltas(PERSON, "Show me the plan", item_id="item_a"),
        {"type": "session.commentary.appended", "text": words, "item_id": "item_a"},
        {"type": "session.commentary.appended", "delta": words},
        *_deltas(PERSON, "for the bakery.", item_id="item_a"),
        delegated,
    ])
    transcript = planner.calls[0]["transcript"]
    assert transcript == [("person", "Show me the plan for the bakery.")]
    assert words not in json.dumps(transcript)


def test_the_window_stays_bounded():
    _, delegated = _delegation()
    events: list[dict] = []
    for turn in range(live.TRANSCRIPT_LINES + 30):
        events.append({"type": PERSON, "delta": f"line {turn}", "item_id": f"item_{turn}"})
    events.append({"type": PERSON, "delta": "x" * (live.TRANSCRIPT_SEGMENT_MAX + 50),
                   "item_id": "item_long"})
    events.append(delegated)
    _, _, planner = _run(events)
    transcript = planner.calls[0]["transcript"]
    assert len(transcript) == live.TRANSCRIPT_LINES
    assert transcript[-1] == ("person", "x" * live.TRANSCRIPT_SEGMENT_MAX)
    assert transcript[0] == ("person", "line 31")


def test_a_turn_with_no_text_at_all_still_asks_again_and_logs_no_text(caplog):
    words = "secret-plan-" + secrets.token_hex(4)
    _, delegated = _delegation()
    caplog.set_level(logging.DEBUG, logger="swarm_voice.live")
    _, socket, planner = _run([
        {"type": "session.input_audio.append"},
        # Deltas with nothing readable in them: no line, and only their keys are logged.
        {"type": PERSON, "item_id": "item_a", "audio": words},
        {"type": PERSON, "item_id": "item_a", "delta": "   "},
        {"type": VOICE, "response_id": "resp_a", "content": words},
        delegated,
    ])
    assert planner.calls == []
    assert _said(socket) == [live.SAY_ASK_AGAIN]
    assert [r for r in caplog.records if "empty transcript" in r.getMessage()]
    keys = [r.getMessage() for r in caplog.records if "no text field" in r.getMessage()]
    assert len(keys) == 1 and "audio" in keys[0]
    assert words not in caplog.text


def test_no_delta_text_is_logged(caplog):
    words = "my-plan-" + secrets.token_hex(4)
    _, delegated = _delegation()
    caplog.set_level(logging.DEBUG, logger="swarm_voice.live")
    _run([*_deltas(PERSON, f"Plan {words}", item_id="item_a"), delegated])
    assert words not in caplog.text


def test_the_delta_families_are_recognised_by_side():
    assert live.delta_speaker(PERSON) == "person"
    assert live.delta_speaker(VOICE) == "voice"
    assert live.delta_speaker("conversation.item.input_audio_transcription.delta") == "person"
    assert live.delta_speaker("response.output_audio_transcript.delta") == "voice"
    assert live.delta_speaker("response.output_transcript.delta") == "voice"
    assert live.delta_speaker("session.output_audio.delta") is None
    assert live.delta_speaker("session.commentary.appended") is None
    assert live.delta_speaker("session.input_transcript.done") is None
