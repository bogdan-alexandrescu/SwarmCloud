"""A GPT-Live planner turn hears the person's request whatever the sideband
calls its transcript finals (live.py, "THE PLANNER HEARD NOTHING").

THE DEFECT (live use, 10 November 2026). A person asked the voice to help build
a website; every planner turn drew nothing and read only the inbox, and the
voice said nothing had come back. `session.delegation.created` carries no task
text, and the planner's window was filled from five exact event names only, so
finals under other names left it empty. This holds:

* finals under other names of the same families fill the planner's transcript,
  by side, and the person's still reach the read-back gate;
* text a delegation event carries of its own reaches the planner turn as its
  request;
* a turn with an empty transcript and no delegation text runs no planner (so
  makes no read), speaks one ask-again line, and logs the counts of the event
  TYPES seen, never their content.

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
from swarm_voice import prompts  # noqa: E402


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
        self.calls.append(kwargs)
        return pl.TurnResult(kwargs["delegation_id"], "It's on screen.")

    def forget(self, session_id: str) -> None:
        pass


def _delegation(**extra) -> tuple[str, dict]:
    delegation_id = "item_" + secrets.token_hex(6)
    return delegation_id, {"type": live.DELEGATION_CREATED,
                           "delegation": {"id": delegation_id, "type": "delegation", **extra}}


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


def test_finals_under_other_names_fill_the_planners_transcript_by_side():
    delegation_id, delegated = _delegation()
    _, socket, planner = _run([
        {"type": "session.usage.updated", "usage": {"seconds": 1}},
        # MUTATION: keep only TRANSCRIPT_FINAL's exact names and none of these is heard.
        {"type": "session.input_audio_transcription.completed", "transcript": "Help me build a website."},
        {"type": "session.output_audio_transcript.done", "text": "Sure, working on it."},
        {"type": "session.user_transcript.completed", "text": "A small one for a bakery."},
        {"type": "session.response.transcript.done", "transcript": "On it."},
        # Not finals: deltas and other events stay out of the window.
        {"type": "session.input_transcript.delta", "delta": "Help"},
        {"type": "session.delegation.transcript.done", "transcript": "neither side"},
        delegated,
    ])
    assert len(planner.calls) == 1
    assert planner.calls[0]["transcript"] == [
        ("person", "Help me build a website."),
        ("voice", "Sure, working on it."),
        ("person", "A small one for a bakery."),
        ("voice", "On it."),
    ]
    assert planner.calls[0]["delegation_id"] == delegation_id
    assert _said(socket) == ["It's on screen."]


def test_the_documented_names_are_still_heard():
    for kind, side in live.TRANSCRIPT_FINAL.items():
        assert live.final_speaker(kind) == side
    assert live.final_speaker("session.updated") is None
    assert live.final_speaker("response.output_audio_transcript.delta") is None


def test_a_delegation_events_own_text_reaches_the_planner_turn():
    _, delegated = _delegation(arguments=json.dumps({"task": "Plan a website for a bakery."}),
                               description="Website plan")
    # MUTATION: drop `delegation_request` and the planner is handed no request.
    _, _, planner = _run([{"type": "session.updated"}, delegated])
    assert len(planner.calls) == 1
    call = planner.calls[0]
    assert call["transcript"] == []
    assert call["request"] == "Plan a website for a bakery.\nWebsite plan"


def test_the_request_is_fenced_into_the_planners_turn_message():
    message = prompts.turn_message([], request="Plan a website for a bakery.")
    assert '<data source="delegation">\nPlan a website for a bakery.\n</data>' in message
    assert "delegation" not in prompts.turn_message([("person", "hi")])


def test_an_empty_turn_asks_again_reads_nothing_and_logs_only_event_types(caplog):
    words = "secret-plan-" + secrets.token_hex(4)
    _, delegated = _delegation()
    caplog.set_level(logging.WARNING, logger="swarm_voice.live")
    _, socket, planner = _run([
        {"type": "session.usage.updated"},
        {"type": "session.usage.updated"},
        {"type": "session.something.else", "text": words},
        delegated,
    ])
    # MUTATION: hand the empty window to the planner and it reads the inbox
    # and reports "nothing", as the owner heard.
    assert planner.calls == []
    assert _said(socket) == [live.SAY_ASK_AGAIN]
    warnings = [r.getMessage() for r in caplog.records if "empty transcript" in r.getMessage()]
    assert len(warnings) == 1
    assert "session.usage.updated x2" in warnings[0]
    assert "session.delegation.created x1" in warnings[0]
    assert words not in caplog.text
