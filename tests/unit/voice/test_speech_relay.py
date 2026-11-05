"""swarm-api's speech relay (docs/design/voice-it-own-speech.md §3.3, §4.2-§4.5;
swarm_api/voice/speech_relay.py).

Readiness and its status words, a final turned into the front's spoken line,
a delegated turn's planner line spoken after the front's, the read-back
matcher first, rotation, idle, a crash recovered and not, a closed tab, a
stale generation. The browser and the speech server are in-memory sockets,
the lease is the real `SpeechMachine` on the in-memory Firestore, and time
is a fake clock: no network, no cloud credential.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from control_plane.fakes import FakeFirestore  # noqa: E402
from test_speech_machine import (  # noqa: E402
    ALICE,
    BOB,
    CHOSEN,
    Clock,
    FakeCompute,
    _lease,
    _machine,
    _mark_live,
)

from swarm_api.voice import speech_machine as sm  # noqa: E402
from swarm_api.voice import speech_relay as sr  # noqa: E402

VOICE = "af_heart"


class Pipe:
    """One socket end: what the relay sends lands in `sent`; what the test
    (or the fake server) puts in `inbox` is what the relay receives."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[Any] = []
        self.closed = False

    async def send(self, data: str | bytes) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(data)
        await self.on_send(data)

    async def on_send(self, data: str | bytes) -> None:
        return None

    async def recv(self) -> str | bytes | None:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(f) for f in self.sent if isinstance(f, str)]

    def statuses(self) -> list[str]:
        return [f["state"] for f in self.frames() if f.get("t") == "status"]


class FakeVm(Pipe):
    """The speech server: boot frames then ready; a final for each utterance;
    speech for each `speak`."""

    def __init__(self, phases: tuple[str, ...] = ("loading models",), *, ready: bool = True,
                 heard: str = "what is running") -> None:
        super().__init__()
        for phase in phases:
            self.inbox.put_nowait(json.dumps({"t": "boot", "phase": phase}))
        if ready:
            self.inbox.put_nowait(json.dumps({"t": "ready", "timings": {}}))
        self.heard = heard
        self.audio_in = 0
        self.spoken: list[str] = []

    async def on_send(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            self.audio_in += len(data)
            return
        message = json.loads(data)
        if message["t"] == "utt.end":
            self.inbox.put_nowait(json.dumps({"t": "final", "seq": message["seq"], "text": self.heard}))
        elif message["t"] == "speak":
            self.spoken.append(message["text"])
            assert message["voice"] == VOICE
            self.inbox.put_nowait(json.dumps({"t": "speak.start", "id": message["id"], "rate": 24000}))
            self.inbox.put_nowait(b"\x00\x01" * 1200)
            self.inbox.put_nowait(json.dumps({"t": "speak.end", "id": message["id"], "cancelled": False}))


class Dialer:
    def __init__(self, *vms: FakeVm | None) -> None:
        self.vms = list(vms)
        self.dials = 0

    async def __call__(self) -> FakeVm:
        self.dials += 1
        if not self.vms:
            raise ConnectionRefusedError("port closed")
        vm = self.vms.pop(0)
        if vm is None:
            raise ConnectionRefusedError("port closed")
        return vm


class FakeTurns:
    def __init__(self, *, reply: str = "On it.", delegation: str | None = None,
                 planner_line: str | None = "Two agents are running.") -> None:
        self.reply, self.delegation_id, self.planner_line = reply, delegation, planner_line
        self.turns: list[str] = []
        self.asked: list[str] = []

    def turn(self, session_id: str, text: str, anchor: dict[str, Any] | None) -> sr.FrontLine:
        assert session_id == ALICE.session_id
        self.turns.append(text)
        return sr.FrontLine(reply=self.reply, delegation_id=self.delegation_id)

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        assert timeout == sr.DELEGATION_SECONDS
        self.asked.append(delegation_id)
        return self.planner_line


async def _direct(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class Harness:
    def __init__(self, *, dialer: Dialer, turns: Any = None, image_changed: bool = False,
                 readback: Any = None) -> None:
        self.db = FakeFirestore()
        _mark_live(self.db, ALICE)
        _mark_live(self.db, BOB)
        self.compute, self.clock, self.applied = FakeCompute(), Clock(), []
        self.machine = _machine(self.db, self.compute, self.clock, self.applied)
        if image_changed:
            self.compute.metadata = {}
        else:
            self.compute.metadata = sm.wanted_metadata(CHOSEN, sm.SpeechConfig().image)
        self.started = self.machine.start(ALICE, chosen=CHOSEN)
        self.browser = Pipe()
        self.dialer = dialer
        self.turns = turns or FakeTurns()
        self.ended: list[str] = []
        self.down: list[str] = []

        async def on_end(cause: str) -> None:
            self.ended.append(cause)
            self.machine.end_session(self.started.holder, cause=cause, ender=None)

        async def on_down(reason: str) -> None:
            self.down.append(reason)
            self.machine.stop(self.started.holder, cause=reason)

        async def sleep(seconds: float) -> None:
            # The connect's retries move time; the clock's one-second tick
            # does not, so the test decides when time passes.
            if seconds >= sr.RETRY_SECONDS:
                self.clock.advance(seconds)
            await asyncio.sleep(0)

        self.relay = sr.SpeechRelay(
            browser=self.browser, dial=dialer, machine=self.machine, holder=self.started.holder,
            turns=self.turns, voice=VOICE, answer_readback=readback, on_end=on_end,
            on_engine_down=on_down, started=sm.SpeechMachine.started_of(_lease(self.db)),
            clock=self.clock.clock, sleep=sleep, to_thread=_direct)

    async def until(self, predicate, what: str) -> None:
        for _ in range(5000):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError(f"never saw {what}; browser got {self.browser.frames()}")

    async def ready(self) -> asyncio.Task:
        task = asyncio.ensure_future(self.relay.run())
        await self.until(lambda: sr.READY in self.browser.statuses(), "ready")
        return task

    async def utterance(self, seq: int = 1) -> None:
        self.browser.inbox.put_nowait(json.dumps({"t": "utt.start", "seq": seq}))
        self.browser.inbox.put_nowait(b"\x10\x00" * 320)
        self.browser.inbox.put_nowait(json.dumps({"t": "utt.end", "seq": seq}))

    async def close(self, task: asyncio.Task) -> str:
        self.browser.inbox.put_nowait(None)
        return await asyncio.wait_for(task, 5)


def run(coro):
    return asyncio.run(coro)


# ---- readiness (§4.2, §4.3) --------------------------------------------------


def test_the_page_reads_the_boot_phases_then_ready_and_settings_are_applied():
    async def go():
        vm = FakeVm(("fetching model qwen3-asr-0.6b", "loading models", "warming up"))
        h = Harness(dialer=Dialer(None, vm))
        task = await h.ready()
        assert h.browser.statuses() == [sr.STARTING_MACHINE, sr.FETCHING, sr.LOADING, sr.READY]
        lease = _lease(h.db)
        assert lease["state"] == sm.RUNNING
        assert {"start.boot", "start.server", "start.load"} <= set(lease["start_phases"])
        assert h.applied == [(h.started.applied, h.started.image)]
        assert await h.close(task) == sr.ENDED_BROWSER
        assert _lease(h.db)["detached_at"] is not None
    run(go())


def test_a_new_speech_server_digest_reads_as_updating_until_the_socket_answers():
    async def go():
        h = Harness(dialer=Dialer(None, None, FakeVm()), image_changed=True)
        task = await h.ready()
        assert h.browser.statuses()[0] == sr.UPDATING
        await h.close(task)
    run(go())


def test_a_server_never_ready_in_four_minutes_is_engine_down_and_the_machine_stops():
    async def go():
        h = Harness(dialer=Dialer())
        why = await asyncio.wait_for(h.relay.run(), 5)
        assert why == sr.ENDED_ENGINE_DOWN
        assert h.browser.statuses()[-1] == sr.ENGINE_DOWN and h.browser.closed
        assert h.down == [sm.FALLBACK_ENGINE_DOWN]
        assert h.compute.status == sm.TERMINATED
        assert sr.READY not in h.browser.statuses()
    run(go())


def test_a_failed_boot_is_engine_down():
    async def go():
        h = Harness(dialer=Dialer(FakeVm(("loading models", "failed"), ready=False)))
        assert await asyncio.wait_for(h.relay.run(), 5) == sr.ENDED_ENGINE_DOWN
        assert h.down == [sm.FALLBACK_ENGINE_DOWN]
    run(go())


# ---- a final is a turn, spoken (§3.3 steps 3-5) ------------------------------


def test_a_final_reaches_the_front_and_its_line_is_spoken():
    async def go():
        vm = FakeVm()
        h = Harness(dialer=Dialer(vm))
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: any(f.get("t") == "say.end" for f in h.browser.frames()), "say.end")
        assert vm.audio_in == 640, "the utterance's audio did not reach the speech server"
        assert h.turns.turns == ["what is running"]
        assert vm.spoken == ["On it."]
        frames = h.browser.frames()
        assert {"t": "final", "seq": 1, "text": "what is running"} in frames
        say = next(f for f in frames if f.get("t") == "say.start")
        assert say["text"] == "On it."
        assert any(isinstance(f, bytes) for f in h.browser.sent), "no speech reached the page"
        await h.close(task)
    run(go())


def test_a_delegated_turn_speaks_the_planner_line_after_the_fronts():
    async def go():
        vm = FakeVm()
        turns = FakeTurns(reply="Looking at that now.", delegation="dl_" + secrets.token_hex(4))
        h = Harness(dialer=Dialer(vm), turns=turns)
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: len(vm.spoken) == 2, "the planner's line")
        assert vm.spoken == ["Looking at that now.", "Two agents are running."]
        assert turns.asked == [turns.delegation_id]
        await h.close(task)
    run(go())


def test_the_default_turns_speak_only_the_fronts_line_without_a_delegation():
    class Client:
        def __init__(self) -> None:
            self.turns: list[str] = []

        def turn(self, session_id: str, text: str) -> Any:
            self.turns.append(text)
            return type("TurnAnswer", (), {"turn_id": "t1", "reply": "Nothing is running.",
                                           "cards": []})()

    async def go():
        vm, client = FakeVm(), Client()
        h = Harness(dialer=Dialer(vm), turns=sr.ClientTurns(client))
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: vm.spoken, "the front's line")
        for _ in range(50):
            await asyncio.sleep(0)
        assert vm.spoken == ["Nothing is running."] and client.turns == ["what is running"]
        await h.close(task)
    run(go())


def test_the_default_turns_ask_the_clients_delegation_when_it_has_one():
    class Client:
        def turn(self, session_id: str, text: str) -> Any:
            return type("A", (), {"reply": "On it.", "delegation_id": "dl_1"})()

        def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str:
            return "Done."

    turns = sr.ClientTurns(Client())
    line = turns.turn(ALICE.session_id, "x", None)
    assert line == sr.FrontLine(reply="On it.", delegation_id="dl_1")
    assert turns.delegation(ALICE.session_id, "dl_1", timeout=1.0) == "Done."


def test_a_read_back_answer_never_reaches_the_front():
    async def go():
        heard: list[str] = []

        async def readback(text: str) -> bool:
            heard.append(text)
            return False  # "go": answered the pending read-back

        vm = FakeVm(heard="go ahead")
        h = Harness(dialer=Dialer(vm), readback=readback)
        task = await h.ready()
        await h.utterance()
        await h.until(lambda: heard, "the read-back matcher")
        for _ in range(50):
            await asyncio.sleep(0)
        assert heard == ["go ahead"] and h.turns.turns == [] and vm.spoken == []
        await h.close(task)
    run(go())


def test_barge_in_cancels_the_speech():
    async def go():
        vm = FakeVm()
        h = Harness(dialer=Dialer(vm))
        task = await h.ready()
        h.browser.inbox.put_nowait(json.dumps({"t": "barge"}))
        await h.until(lambda: {"t": "cancel"} in [json.loads(f) for f in vm.sent
                                                  if isinstance(f, str)], "cancel")
        await h.close(task)
    run(go())


# ---- rotation, heartbeat, idle (§3.3, §4.4) ----------------------------------


def test_the_socket_rotates_at_540_seconds():
    async def go():
        h = Harness(dialer=Dialer(FakeVm()))
        task = await h.ready()
        assert {"t": "rotate"} not in h.browser.frames()
        h.clock.advance(sr.ROTATE_SECONDS)
        await h.until(lambda: {"t": "rotate"} in h.browser.frames(), "rotate")
        await h.close(task)
    run(go())


def test_the_heartbeat_is_written_on_the_lease_every_30_seconds():
    async def go():
        h = Harness(dialer=Dialer(FakeVm()))
        task = await h.ready()
        await h.until(lambda: h.relay.socket_id in (_lease(h.db).get("sockets") or {}), "attach")
        first = _lease(h.db)["heartbeat_at"]
        h.clock.advance(sm.HEARTBEAT_SECONDS)
        await h.until(lambda: _lease(h.db)["heartbeat_at"] > first, "a second heartbeat")
        await h.close(task)
    run(go())


def test_idle_for_the_idle_time_ends_the_session_and_stops_the_machine():
    async def go():
        h = Harness(dialer=Dialer(FakeVm()))
        task = await h.ready()
        h.clock.advance(h.started.idle_seconds + sm.HEARTBEAT_SECONDS)
        assert await asyncio.wait_for(task, 5) == sr.ENDED_SESSION
        assert h.ended == [sm.END_IDLE]
        assert h.compute.status == sm.TERMINATED and _lease(h.db)["state"] == sm.STOPPED
        assert {"t": "ended", "cause": sm.END_IDLE} in h.browser.frames()
    run(go())


# ---- a crash, a closed tab, a stale generation (§4.5, invariant 5) -----------


def test_a_crashed_speech_server_is_reconnected_and_counted():
    async def go():
        first = FakeVm()
        h = Harness(dialer=Dialer(first, None, FakeVm(())))
        task = await h.ready()
        first.inbox.put_nowait(None)  # the container restarted
        await h.until(lambda: h.relay.state.crashes == 1, "the recovery")
        statuses = h.browser.statuses()
        assert statuses[-2:] == [sr.RECONNECTING, sr.READY]
        assert h.down == []
        await h.close(task)
    run(go())


def test_a_speech_server_not_back_in_three_minutes_is_engine_down():
    async def go():
        first = FakeVm()
        h = Harness(dialer=Dialer(first))
        task = await h.ready()
        first.inbox.put_nowait(None)
        assert await asyncio.wait_for(task, 5) == sr.ENDED_ENGINE_DOWN
        assert h.browser.statuses()[-1] == sr.ENGINE_DOWN
        assert h.down == [sm.FALLBACK_ENGINE_DOWN] and h.compute.status == sm.TERMINATED
        assert h.dialer.dials >= sr.CRASH_SECONDS / sr.RETRY_SECONDS
    run(go())


def test_a_closed_tab_is_ended_after_a_minute_unless_it_reconnects():
    async def go():
        h = Harness(dialer=Dialer(FakeVm()))
        task = await h.ready()
        assert await h.close(task) == sr.ENDED_BROWSER
        ended: list[str] = []

        async def on_end(cause: str) -> None:
            ended.append(cause)

        async def minute(seconds: float) -> None:
            h.clock.advance(seconds)

        # A reload re-attached within the minute: nothing ends.
        async def minute_then_reload(seconds: float) -> None:
            h.machine.attach(h.started.holder, socket_id="sk_reload")
            h.clock.advance(seconds)

        assert await sr.wait_for_reconnect(h.machine, h.started.holder, on_end=on_end,
                                           sleep=minute_then_reload, to_thread=_direct) is False
        h.machine.detach(h.started.holder, socket_id="sk_reload")
        assert await sr.wait_for_reconnect(h.machine, h.started.holder, on_end=on_end,
                                           sleep=minute, to_thread=_direct) is True
        assert ended == [sm.END_TAB_CLOSED]
    run(go())


def test_a_stale_generation_closes_the_relay_without_touching_the_machine():
    async def go():
        h = Harness(dialer=Dialer(FakeVm()))
        task = await h.ready()
        # Another start took the machine (its lease went stale and was taken).
        h.db.docs[f"{sm.COLLECTION}/{sm.LEASE}"]["generation"] += 1
        h.clock.advance(sm.HEARTBEAT_SECONDS)
        assert await asyncio.wait_for(task, 5) == sr.ENDED_STALE
        assert "stop" not in h.compute.names()
        assert h.ended == [] and h.down == [] and h.browser.closed
    run(go())


@pytest.mark.parametrize("phase,status", [
    ("fetching model kokoro-82m", sr.FETCHING), ("loading models", sr.LOADING),
    ("warming up", sr.LOADING)])
def test_boot_phases_map_to_the_pages_status_words(phase, status):
    assert sr._boot_status(phase) == status
