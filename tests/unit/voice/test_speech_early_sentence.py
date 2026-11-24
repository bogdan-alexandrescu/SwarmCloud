"""Own speech's two latency cuts in swarm-api's relay
(docs/design/voice-it-own-speech.md §3.3 steps 4-5, §12.6;
swarm_api/voice/speech_relay.py).

(a) The front's first sentence goes to speech as soon as it is complete, while
    the front's turn is still running; the rest of the reply follows, and the
    early sentence is never spoken a second time. A barge-in cuts it, and
    nothing of that turn is spoken after the cut.
(b) A delegated (planner) line wakes the relay's line speaker at once, with no
    tick to wait for; a lost wake-up is caught by the slow fallback recheck
    and a duplicated one, or the same delegation arriving twice, speaks the
    line once.

The browser and the speech server are the in-memory sockets of
test_speech_relay.py, time is its fake clock, and the front runs in a real
worker thread (as `asyncio.to_thread` runs it in production) so that "before
the turn's last token" is a real ordering: no audio, no network, no cloud
credential.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_speech_relay import ALICE, Dialer, FakeVm, Harness, run  # noqa: E402

from swarm_api.voice import speech_relay as sr  # noqa: E402

FIRST = "Two agents are running."
REST = "One of them is the login fix."
REPLY = f"{FIRST} {REST}"


class StreamingTurns:
    """A front that hands its first sentence out mid-turn (`on_sentence`),
    then holds its last token until `release` is set (or the early sentence
    reached the speech server, when `wait_for_speak` is given)."""

    streams_sentences = True

    def __init__(self, *, reply: str = REPLY, first: str | None = FIRST,
                 delegation: str | None = None, planner_line: str | None = None,
                 wait_for_speak: threading.Event | None = None) -> None:
        self.reply, self.first = reply, first
        self.delegation_id, self.planner_line = delegation, planner_line
        self.wait_for_speak = wait_for_speak
        self.release = threading.Event()
        #: True when the early sentence was already at the speech server when
        #: the turn's last token came.
        self.spoken_before_last_token: bool | None = None
        self.turns: list[str] = []
        self.asked: list[str] = []

    def turn(self, session_id: str, text: str, anchor: dict[str, Any] | None, *,
             on_sentence: Callable[[str], None] | None = None) -> sr.FrontLine:
        assert session_id == ALICE.session_id
        self.turns.append(text)
        if self.first is not None and on_sentence is not None:
            on_sentence(self.first)
        if self.wait_for_speak is not None:
            self.spoken_before_last_token = self.wait_for_speak.wait(5.0)
        else:
            self.release.wait(5.0)
        return sr.FrontLine(reply=self.reply, delegation_id=self.delegation_id, early=self.first)

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        self.asked.append(delegation_id)
        return self.planner_line


class SpeakWatchVm(FakeVm):
    """The fake server, setting `got_speak` when the first `speak` arrives and
    recording every `cancel`."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.got_speak = threading.Event()
        self.cancels = 0

    async def on_send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            message = json.loads(data)
            if message["t"] == "speak":
                self.got_speak.set()
            elif message["t"] == "cancel":
                self.cancels += 1
        await super().on_send(data)


def _threaded(h: Harness) -> None:
    """Run the front in a real worker thread, as production does."""
    h.relay._to_thread = asyncio.to_thread


async def _settle(h: Harness, predicate, what: str) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never saw {what}; browser got {h.browser.frames()}")


def _said(h: Harness) -> list[str]:
    return [f["text"] for f in h.browser.frames() if f.get("t") == "say.start"]


def test_the_first_sentence_reaches_the_speech_call_before_the_turns_last_token():
    async def go():
        vm = SpeakWatchVm()
        turns = StreamingTurns(wait_for_speak=vm.got_speak)
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: len(vm.spoken) == 2, "the rest spoken")
        assert turns.spoken_before_last_token is True, "the first sentence waited for the turn's end"
        assert vm.spoken == [FIRST, REST]
        await h.close(task)
    run(go())


def test_the_early_sentence_lowers_the_recorded_front_ms():
    """§12.6's `front_ms` (the final to the reply handed to synthesis) is
    stamped at the early sentence, not at the turn's end."""
    async def go():
        vm = SpeakWatchVm()
        turns = StreamingTurns()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        frames: list[dict[str, Any]] = []
        h.relay._on_frame = frames.append
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: vm.spoken, "the early sentence")
        # The front's turn is still running, and takes 0.8 s more on the clock.
        h.clock.advance(0.8)
        turns.release.set()
        await _settle(h, lambda: len(vm.spoken) == 2, "the rest spoken")
        [record] = [f for f in frames if f.get("t") == "timing" and f.get("kind") == "turn"]
        assert record["front_ms"] == 0.0, record
        await h.close(task)
    run(go())


def test_nothing_is_spoken_twice_and_the_browser_sees_the_reply_once():
    async def go():
        vm = SpeakWatchVm()
        turns = StreamingTurns()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: vm.spoken, "the early sentence")
        turns.release.set()
        await _settle(h, lambda: len(vm.spoken) == 2, "the rest spoken")
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert vm.spoken == [FIRST, REST]
        # What the page shows (and the transcript records) is the reply, once.
        assert " ".join(_said(h)) == REPLY
        await h.close(task)
    run(go())


def test_a_reply_that_is_only_its_first_sentence_is_spoken_once():
    async def go():
        vm = SpeakWatchVm()
        turns = StreamingTurns(reply=FIRST)
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: vm.spoken, "the early sentence")
        turns.release.set()
        await _settle(h, lambda: turns.turns and not h.relay.state.turns_in_flight, "the turn's end")
        assert vm.spoken == [FIRST]
        await h.close(task)
    run(go())


def test_an_interruption_cuts_an_early_sentence_and_the_rest_is_never_spoken():
    async def go():
        vm = SpeakWatchVm()
        turns = StreamingTurns()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: vm.spoken, "the early sentence")
        # The person barges in while the early sentence plays.
        h.browser.inbox.put_nowait(json.dumps({"t": "barge"}))
        await _settle(h, lambda: vm.cancels == 1, "the cancel")
        turns.release.set()
        await _settle(h, lambda: not h.relay.state.turns_in_flight, "the turn's end")
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert vm.spoken == [FIRST], "speech continued after the interruption"
        await h.close(task)
    run(go())


def test_an_early_sentence_that_comes_after_an_interruption_is_never_spoken():
    async def go():
        vm = SpeakWatchVm()
        gate = threading.Event()

        class Late(StreamingTurns):
            def turn(self, session_id, text, anchor, *, on_sentence=None):  # noqa: ANN001
                gate.wait(5.0)
                return super().turn(session_id, text, anchor, on_sentence=on_sentence)

        turns = Late()
        turns.release.set()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: sr.THINKING in h.browser.statuses(), "thinking")
        h.browser.inbox.put_nowait(json.dumps({"t": "barge"}))
        await _settle(h, lambda: vm.cancels == 1, "the cancel")
        gate.set()
        await _settle(h, lambda: turns.turns and not h.relay.state.turns_in_flight, "the turn's end")
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert vm.spoken == []
        await h.close(task)
    run(go())


def test_a_stop_cuts_an_early_sentence_already_sent_to_speech():
    async def go():
        vm = SpeakWatchVm()
        turns = StreamingTurns()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: vm.spoken, "the early sentence")
        # Stop: the browser closes; the relay closes the speech socket, which
        # ends every synthesis on it, and speaks nothing more.
        await h.browser.close()
        turns.release.set()
        await asyncio.wait_for(task, 5.0)
        assert vm.closed
        assert vm.spoken == [FIRST]
    run(go())


def test_a_delegated_line_is_relayed_without_waiting_for_a_tick():
    async def go():
        vm = FakeVm()
        turns = StreamingTurns(reply="On it.", first=None, delegation="vd_1",
                               planner_line="The plan is on screen.")
        turns.release.set()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        # No fallback recheck at all: only the wake-up can speak the line.
        h.relay._line_recheck = None
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: "The plan is on screen." in vm.spoken, "the planner's line")
        assert vm.spoken == ["On it.", "The plan is on screen."]
        await h.close(task)
    run(go())


def test_a_lost_wake_up_is_caught_by_the_fallback_recheck():
    async def go():
        vm = FakeVm()
        turns = StreamingTurns(reply="On it.", first=None, delegation="vd_1",
                               planner_line="The plan is on screen.")
        turns.release.set()
        h = Harness(dialer=Dialer(vm), turns=turns)
        _threaded(h)
        h.relay._line_recheck = 0.05
        h.relay._wake.set = lambda: None  # every wake-up is lost
        task = await h.ready()
        await h.utterance()
        await _settle(h, lambda: "The plan is on screen." in vm.spoken, "the planner's line")
        assert vm.spoken.count("The plan is on screen.") == 1
        await h.close(task)
    run(go())


def test_a_duplicated_wake_up_or_line_speaks_it_once():
    async def go():
        vm = FakeVm()
        h = Harness(dialer=Dialer(vm), turns=StreamingTurns(first=None))
        task = await h.ready()
        relay = h.relay
        relay.line_arrived("vd_1", "The plan is on screen.")
        relay.line_arrived("vd_1", "The plan is on screen.")
        relay._wake.set()
        relay._wake.set()
        await h.until(lambda: vm.spoken, "the planner's line")
        for _ in range(50):
            await asyncio.sleep(0)
        assert vm.spoken == ["The plan is on screen."]
        await h.close(task)
    run(go())


def test_a_delegated_line_waits_for_the_person_to_finish_then_is_spoken_once():
    async def go():
        vm = FakeVm()
        turns = StreamingTurns(first=None)
        turns.release.set()  # the front answers at once: the loop never blocks on it
        h = Harness(dialer=Dialer(vm), turns=turns)
        task = await h.ready()
        h.browser.inbox.put_nowait(json.dumps({"t": "utt.start", "seq": 7}))
        await h.until(lambda: h.relay.state.utterance == 7, "the utterance")
        h.relay.line_arrived("vd_2", "Done.")
        for _ in range(50):
            await asyncio.sleep(0)
        assert vm.spoken == [], "a line was spoken over the person"
        h.browser.inbox.put_nowait(json.dumps({"t": "utt.end", "seq": 7}))
        await h.until(lambda: "Done." in vm.spoken, "the line at the silence")
        for _ in range(50):
            await asyncio.sleep(0)
        assert vm.spoken.count("Done.") == 1
        await h.close(task)
    run(go())
