"""The GPT voice engine hears the person in WHOLE UTTERANCES, not words
(live.py, "WHOLE UTTERANCES"; planner.py, "WHAT IT DREW IS SAID").

THE DEFECT (owner, 10 November 2026, on the GPT voice engine): the voice acted
on each word, mid-sentence, and the read-back gate was posted only on a person
final, which the Live sideband never sends. And the voice said "there is a card
on screen" when the planner had drawn none. This holds:

* the person's deltas form one utterance until its text ends a sentence and
  UTTERANCE_END_GAP_SECONDS pass with no new delta of theirs, or
  UTTERANCE_IDLE_GAP_SECONDS pass whatever its text; a comma never ends one;
* a new item, a change of speaker or a delegation ends one at once;
* each utterance reaches the planner's transcript as one line and swarm-api's
  read-back gate once, in one piece;
* the instructions sent at create carry the turn-taking setting;
* a planner turn that drew nothing says so, and the voice is told to point at
  a card only when the result says one was drawn.

Offline: a LiveSession reads a scripted socket on an injected clock; the
planner and the read-back gate are fakes.
"""

from __future__ import annotations

import asyncio
import json
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


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Wait:
    """A pause in the script: the clock moves on `seconds`, then an event the
    session ignores arrives (the sideband's audio events never stop)."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class _State:
    name = "OPEN"


class ScriptedSocket:
    def __init__(self, script: list, clock: Clock) -> None:
        self._script = script
        self._clock = clock
        self.sent: list[dict] = []
        self.state = _State()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for step in self._script:
            if isinstance(step, Wait):
                self._clock.now += step.seconds
                yield json.dumps({"type": "session.input_audio.append"})
            else:
                yield json.dumps(step)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        pass


class Gate:
    """swarm-api's read-back gate (readback.Finals): records each post."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def post(self, grant, text: str):
        self.posted.append(text)
        return None


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def delegate(self, **kwargs):
        self.calls.append({**kwargs, "transcript": list(kwargs["transcript"])})
        return pl.TurnResult(kwargs["delegation_id"], pl.NOTHING_DRAWN)

    def forget(self, session_id: str) -> None:
        pass


def _deltas(kind: str, words: str, **ids) -> list[dict]:
    """`words` as the sideband sends it: a delta a word, spaces kept."""
    parts = words.split(" ")
    pieces = [part + " " for part in parts[:-1]] + [parts[-1]]
    return [{"type": kind, "delta": piece, **ids} for piece in pieces]


def _delegated() -> dict:
    return {"type": live.DELEGATION_CREATED,
            "delegation": {"id": "item_" + secrets.token_hex(6), "type": "delegation"}}


def _run(script: list) -> tuple[live.LiveSession, Gate, FakePlanner]:
    clock, gate, planner = Clock(), Gate(), FakePlanner()
    socket = ScriptedSocket(script, clock)

    async def main() -> live.LiveSession:
        session = live.LiveSession("vs_" + secrets.token_hex(4), "live_" + secrets.token_hex(4),
                                   credential=None, api=None, on_event=None,
                                   on_closed=lambda s: None, person="owner@example.com",
                                   planner=planner, finals=gate, clock=clock)
        session.grant = object()
        session._socket = socket
        await session._read(socket)
        for _ in range(200):
            if not session._turns:
                break
            await asyncio.sleep(0.01)
        return session

    return asyncio.run(asyncio.wait_for(main(), 10)), gate, planner


def test_deltas_forming_two_sentences_with_gaps_are_two_utterances():
    session, gate, planner = _run([
        *_deltas(PERSON, "Draw me a home page.", item_id="item_a"),
        # MUTATION: never end an utterance on a gap and this is one line, posted never.
        Wait(live.UTTERANCE_END_GAP_SECONDS + 0.1),
        *_deltas(PERSON, "Make it blue, with a big header.", item_id="item_a"),
        Wait(live.UTTERANCE_END_GAP_SECONDS + 0.1),
        _delegated(),
    ])
    assert gate.posted == ["Draw me a home page.", "Make it blue, with a big header."]
    assert planner.calls[0]["transcript"] == [
        ("person", "Draw me a home page."),
        ("person", "Make it blue, with a big header."),
    ]


def test_a_comma_and_a_short_gap_do_not_end_an_utterance():
    # Longer than the gap after a period, shorter than the idle gap.
    short = (live.UTTERANCE_END_GAP_SECONDS + live.UTTERANCE_IDLE_GAP_SECONDS) / 2
    _, gate, planner = _run([
        *_deltas(PERSON, "Draw me a page for the bakery,", item_id="item_a"),
        # MUTATION: count a comma as a sentence end and the person is cut off here.
        Wait(short),
        *_deltas(PERSON, " and put the opening hours on it.", item_id="item_a"),
        Wait(short),
        _delegated(),
    ])
    assert gate.posted == ["Draw me a page for the bakery, and put the opening hours on it."]
    assert planner.calls[0]["transcript"] == [
        ("person", "Draw me a page for the bakery, and put the opening hours on it.")]


def test_a_short_gap_after_a_period_ends_an_utterance_and_a_shorter_one_does_not():
    _, gate, _ = _run([
        *_deltas(PERSON, "Show me the plan.", item_id="item_a"),
        Wait(live.UTTERANCE_END_GAP_SECONDS / 2),
        *_deltas(PERSON, " The one for the shop.", item_id="item_a"),
        Wait(live.UTTERANCE_END_GAP_SECONDS + 0.05),
        {"type": "session.usage.updated"},
    ])
    assert gate.posted == ["Show me the plan. The one for the shop."]


def test_a_long_gap_without_punctuation_ends_an_utterance():
    _, gate, planner = _run([
        *_deltas(PERSON, "draw me a page for the bakery", item_id="item_a"),
        Wait(live.UTTERANCE_END_GAP_SECONDS + 0.1),
        # Not yet: no end punctuation, so the idle gap has not passed.
        *_deltas(PERSON, " with a menu", item_id="item_a"),
        # MUTATION: only end on punctuation and this never ends.
        Wait(live.UTTERANCE_IDLE_GAP_SECONDS + 0.1),
        {"type": "session.usage.updated"},
        *_deltas(PERSON, "go", item_id="item_a"),
        _delegated(),
    ])
    assert gate.posted == ["draw me a page for the bakery with a menu", "go"]
    assert planner.calls[0]["transcript"] == [
        ("person", "draw me a page for the bakery with a menu"), ("person", "go")]


def test_each_utterance_reaches_the_read_back_gate_once_in_one_piece():
    _, gate, _ = _run([
        *_deltas(PERSON, "Go ahead and start it.", item_id="item_a"),
        Wait(live.UTTERANCE_END_GAP_SECONDS + 0.1),
        Wait(live.UTTERANCE_IDLE_GAP_SECONDS + 0.1),
        # A new item ends the one before at once, and is posted when it ends.
        *_deltas(PERSON, "Not that one", item_id="item_b"),
        *_deltas(PERSON, "the other one", item_id="item_c"),
        Wait(live.UTTERANCE_IDLE_GAP_SECONDS + 0.1),
        # A change of speaker ends an utterance that names no item.
        *_deltas(PERSON, "Wait"),
        *_deltas(VOICE, "Okay."),
        # A handoff ends what is open; a second one posts nothing more.
        *_deltas(PERSON, "Yes"),
        _delegated(),
        _delegated(),
        Wait(live.UTTERANCE_IDLE_GAP_SECONDS + 0.1),
    ])
    # MUTATION: post a word at a time (each delta) and this is a list of words.
    assert gate.posted == ["Go ahead and start it.", "Not that one", "the other one", "Wait", "Yes"]


def test_a_final_ends_its_utterance_and_adds_only_what_was_not_heard():
    _, gate, planner = _run([
        *_deltas(PERSON, "Draw the page.", item_id="item_a"),
        Wait(live.UTTERANCE_END_GAP_SECONDS + 0.1),
        *_deltas(PERSON, "Then plan the lanes", item_id="item_a"),
        {"type": "session.input_transcript.done", "item_id": "item_a",
         "transcript": "Draw the page. Then plan the lanes."},
        _delegated(),
    ])
    assert gate.posted == ["Draw the page.", "Then plan the lanes."]
    assert planner.calls[0]["transcript"] == [
        ("person", "Draw the page."), ("person", "Then plan the lanes.")]


def test_an_utterance_ends_on_its_gap_when_no_other_event_arrives(monkeypatch):
    # Real time, short gaps: the session's own timer ends it.
    monkeypatch.setattr(live, "UTTERANCE_END_GAP_SECONDS", 0.05)
    gate = Gate()

    class Quiet(ScriptedSocket):
        async def _iter(self):
            for raw in _deltas(PERSON, "Cancel the last run.", item_id="item_a"):
                yield json.dumps(raw)
            # The sideband says nothing more for a while.
            await asyncio.sleep(0.4)

    async def main() -> None:
        session = live.LiveSession("vs_" + secrets.token_hex(4), "live_x", credential=None, api=None,
                                   on_event=None, on_closed=lambda s: None, finals=gate)
        session.grant = object()
        socket = Quiet([], Clock())
        reader = asyncio.ensure_future(session._read(socket))
        await asyncio.sleep(0.3)
        posted = list(gate.posted)
        await reader
        assert posted == ["Cancel the last run."]

    asyncio.run(asyncio.wait_for(main(), 10))
    assert gate.posted == ["Cancel the last run."]


def test_the_gaps_are_named_and_a_comma_never_ends_a_sentence():
    assert 0 < live.UTTERANCE_END_GAP_SECONDS < live.UTTERANCE_IDLE_GAP_SECONDS
    assert live.ends_sentence("Draw it.") and live.ends_sentence("Is it done?")
    assert live.ends_sentence('Say "go."') and live.ends_sentence("Stop!")
    assert not live.ends_sentence("Draw it,") and not live.ends_sentence("Draw it")
    assert live.utterance_gap("Draw it,") == live.UTTERANCE_IDLE_GAP_SECONDS
    assert live.utterance_gap("Draw it.") == live.UTTERANCE_END_GAP_SECONDS


def test_the_instructions_ask_the_voice_to_wait_for_a_whole_sentence():
    words = live.TURN_INSTRUCTIONS.lower()
    assert "whole sentence" in words and "pause" in words and "comma" in words


def test_a_turn_that_drew_nothing_says_so_and_the_voice_points_only_at_a_drawn_card():
    nothing = pl.TurnResult("item_1", "")
    assert pl.drew(nothing) == pl.NOTHING_DRAWN
    assert "drew nothing" in pl.NOTHING_DRAWN
    # MUTATION: tell the voice to "point at the card on screen" whatever the result.
    assert "point at the card on screen" not in live.DELEGATE_INSTRUCTIONS
    assert "only when the result" in live.DELEGATE_INSTRUCTIONS
    assert "nothing was drawn" in live.DELEGATE_INSTRUCTIONS
    assert "nothing was drawn" in live.SAY_ASK_AGAIN


def test_what_a_turn_drew_is_counted():
    result = pl.TurnResult("item_1", "", cards=[{"kind": "plan"}, {"kind": "details_input"}],
                           visuals=[{"tool": "show_mockup", "visual_id": "vv_1"},
                                    {"tool": "add_note", "note_id": "vn_1"}])
    assert pl.drew(result) == "I drew 2 cards and 1 mock-up on screen."
    one = pl.TurnResult("item_1", "", visuals=[{"tool": "show_mockup", "visual_id": "vv_1"}])
    assert pl.drew(one) == "I drew 1 mock-up on screen."
