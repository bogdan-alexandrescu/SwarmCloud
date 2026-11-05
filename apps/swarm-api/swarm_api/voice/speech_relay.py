"""swarm-api's relay between the Talk page's audio socket and the speech
machine (docs/design/voice-it-own-speech.md §3.2, §3.3, §4.2-§4.5).

ONE RELAY PER BROWSER SOCKET. The browser sends speech-only 16 kHz PCM frames
and JSON (`utt.start`, `utt.end`, `barge`, `ping`, `point`); the relay opens
its own socket to the speech server's `/v1/stream` with a Google ID token for
the audience `swarm-speech` (§3.2: the subnet proves nothing, the token is the
gate) and passes frames each way:

    browser -> VM   utt.start / PCM / utt.end as sent; `barge` as `cancel`
    VM -> browser   partial, final; speak.start as say.start (with the text
                    and its point_at); speech PCM; speak.end as say.end;
                    status words (§4.3); `rotate` at ROTATE_SECONDS

READINESS (§4.2 steps 3-4). Until the server answers `ready` the page reads
the start's status: `updating speech server` while a new digest is pulled
(the port is closed then), `fetching model` and `loading models` from the
server's own `boot` frames. The four minutes a start has (READY_SECONDS) do
not count `updating speech server` or `fetching model`, which have
UPDATE_SECONDS of their own. A server that reports `failed`, or no ready in
time, is `engine down`: the session falls back to text and the machine stops.

A FINAL IS A TURN (§3.3 step 4). The read-back matcher sees it first
(`answer_readback`: "go" or "not that" answering a pending read-back never
reaches the front). Otherwise it is `Turns.turn`, the fast Claude front's
one-or-two-sentence answer, spoken at once. A turn the front delegated
carries a `delegation_id`; the relay asks `Turns.delegation` for the
planner's line (waiting at most DELEGATION_SECONDS, the planner's
TURN_SECONDS) and speaks it at the next silence, after the front's line. A
newer utterance never waits for it.

THE MEETING POINT WITH THE START LANE (§14 L4 and L6). `Turns` is the
interface: `turn(session_id, text, anchor)` returns a `FrontLine`;
`delegation(session_id, delegation_id, timeout)` returns the planner's line
or None. `ClientTurns` is the working default: the turn goes to swarm-voice's
`/turn` through the session client, a `delegation_id` is read from the
answer when it carries one, and the delegation is asked of the client's
`delegation` (L6's `VoiceSessions.delegation`, the client of swarm-voice's
`GET /delegations/{id}`) when it has one. A turn with no delegation speaks
only the front's line.

THE LEASE (speech_machine). The relay attaches its socket to the lease,
writes the heartbeat and the last activity every HEARTBEAT_SECONDS, and
detaches on close. Activity (§4.4) is an utterance, a turn or delegation in
flight, speech playing and a `point`; pings and partials are not. Every write
names the generation the start took: once it is stale the relay closes
without touching the machine (invariant 5). Idle (§4.4) and a tab closed for
a minute (§4.5) end the session through `on_end`; the sweep is the stop that
does not depend on this process.

A CRASH (§4.5). The VM socket dropping is `reconnecting`, retried every
RETRY_SECONDS for CRASH_SECONDS; recovered is `ready` again and one crash
counted; not recovered is `engine down`.

Nothing the socket carries is logged: not audio, not words, not the token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from . import speech_machine as sm

log = logging.getLogger(__name__)

#: §3.3: swarm-api's requests end at 660 s; the socket rotates at 540 s.
ROTATE_SECONDS = 540.0
#: §4.2 step 3: the VM socket is retried every two seconds.
RETRY_SECONDS = 2.0
#: §4.5: a crashed speech server is retried for three minutes.
CRASH_SECONDS = 180.0
#: §3.3 step 5: a delegation is waited on at most the planner's turn.
DELEGATION_SECONDS = 240.0
#: How often the relay's clock looks at rotation, heartbeat and idle.
TICK_SECONDS = 1.0
#: §8.3: a pointer anchor rides on the utterance when under 5 s old.
ANCHOR_SECONDS = 5.0
MAX_TEXT_FRAME = 16 * 1024
MAX_AUDIO_FRAME = 32 * 1024

# Status words on the page (§4.3), one or two each.
STARTING_MACHINE = "starting machine"
UPDATING = "updating speech server"
FETCHING = "fetching model"
LOADING = "loading models"
READY = "ready"
LISTENING = "listening"
HEARING = "hearing"
THINKING = "thinking"
SPEAKING = "speaking"
RECONNECTING = "reconnecting"
ENGINE_DOWN = "engine down"

# Why a relay ended.
ENDED_BROWSER = "browser_closed"
ENDED_STALE = "stale"
ENDED_ENGINE_DOWN = "engine_down"
ENDED_SESSION = "session_ended"


class Socket(Protocol):
    """One end of a socket: text frames are JSON strings, audio is bytes."""

    async def send(self, data: str | bytes) -> None: ...
    async def recv(self) -> str | bytes | None:
        """The next frame, or None once the socket closed."""
        ...
    async def close(self) -> None: ...


Dial = Callable[[], Awaitable[Socket]]


@dataclass(frozen=True)
class FrontLine:
    """The front's answer to one final: its sentence, and the delegation it
    started when it handed the request to the planner."""

    reply: str
    delegation_id: str | None = None


class Turns(Protocol):
    def turn(self, session_id: str, text: str, anchor: dict[str, Any] | None) -> FrontLine: ...
    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        """The planner's spoken line once the delegation ends; None when it
        has none, or did not end in `timeout`."""
        ...


class ClientTurns:
    """The default `Turns`: swarm-voice's `/turn` through the session client
    (`sessions.HttpVoiceClient.turn`), and the client's own `delegation` when
    the start lane gives it one."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def turn(self, session_id: str, text: str, anchor: dict[str, Any] | None) -> FrontLine:
        answered = self._client.turn(session_id, text)
        delegation = getattr(answered, "delegation_id", None)
        return FrontLine(reply=str(getattr(answered, "reply", "") or ""),
                         delegation_id=delegation if isinstance(delegation, str) and delegation else None)

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        ask = getattr(self._client, "delegation", None)
        if not callable(ask):
            return None
        line = ask(session_id, delegation_id, timeout=timeout)
        return line if isinstance(line, str) and line.strip() else None


def _status(state: str) -> str:
    return json.dumps({"t": "status", "state": state})


def _json(frame: str | bytes) -> dict[str, Any]:
    if not isinstance(frame, str):
        return {}
    try:
        message = json.loads(frame)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


def _boot_status(phase: str) -> str:
    if phase.startswith("fetching model"):
        return FETCHING
    if phase == "updating speech server":
        return UPDATING
    return LOADING


@dataclass
class _State:
    utterance: int | None = None
    speaking: set[str] = field(default_factory=set)
    said: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_lines: list[str] = field(default_factory=list)
    turns_in_flight: int = 0
    anchor: dict[str, Any] | None = None
    anchor_at: float = 0.0
    activity_at: datetime | None = None
    crashes: int = 0
    rotated: bool = False


class SpeechRelay:
    """Relays one browser socket to the speech machine for one session."""

    def __init__(self, *, browser: Socket, dial: Dial, machine: sm.SpeechMachine,
                 holder: sm.Holder, turns: Turns, voice: str,
                 answer_readback: Callable[[str], Awaitable[bool]] | None = None,
                 on_end: Callable[[str], Awaitable[None]] | None = None,
                 on_engine_down: Callable[[str], Awaitable[None]] | None = None,
                 on_frame: Callable[[dict[str, Any]], None] | None = None,
                 started: sm.Started | None = None, budget_seconds: float = sm.READY_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread) -> None:
        self._browser = browser
        self._dial = dial
        self._machine = machine
        self._holder = holder
        self._turns = turns
        self._voice = voice
        self._answer_readback = answer_readback
        self._on_end = on_end
        self._on_engine_down = on_engine_down
        #: The server's `timing` and `metrics` frames, for the telemetry lane.
        self._on_frame = on_frame
        #: The start's record: readiness marks its settings applied. A socket
        #: that re-attaches after a rotation or a restart has none.
        self._started = started
        self._image_changed = bool(started and started.image_changed)
        self._budget = budget_seconds
        self._clock = clock
        self._sleep = sleep
        self._to_thread = to_thread
        self.socket_id = sm.new_socket_id()
        self.state = _State()
        self._vm: Socket | None = None
        self._vm_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._ended: str | None = None

    # -- small helpers

    async def _tell(self, frame: dict[str, Any] | str) -> None:
        try:
            await self._browser.send(frame if isinstance(frame, str) else json.dumps(frame))
        except Exception:  # noqa: BLE001 -- the browser went; its pump sees it
            pass

    async def _to_vm(self, data: str | bytes) -> bool:
        vm = self._vm
        if vm is None:
            return False
        try:
            await vm.send(data)
            return True
        except Exception:  # noqa: BLE001 -- the VM pump sees the drop
            return False

    def _active(self) -> None:
        self.state.activity_at = self._machine.now()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fenced(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """A lease write; a stale generation ends the relay (invariant 5)."""
        try:
            return await self._to_thread(call, *args, **kwargs)
        except sm.StaleLease:
            self._ended = self._ended or ENDED_STALE
            return None

    # -- the VM socket and its boot (§4.2)

    async def _connect(self, *, budget: float, update_budget: float,
                       status: str) -> Socket | None:
        """Dial until the server says ready; None when it never does. Time in
        `updating speech server` and `fetching model` is not counted against
        `budget`, up to `update_budget` of it."""
        began = self._clock()
        held = 0.0
        mark = began
        current = status
        await self._tell(_status(current))

        def spent() -> float:
            nonlocal held, mark
            now = self._clock()
            if current in (UPDATING, FETCHING):
                held = min(update_budget, held + (now - mark))
            mark = now
            return now - began - held

        while self._ended is None and spent() < budget:
            try:
                vm = await self._dial()
            except Exception as exc:  # noqa: BLE001 -- the port is closed while booting
                log.info("speech socket not answering yet (%s)", type(exc).__name__)
                await self._sleep(RETRY_SECONDS)
                continue
            answered = self._clock()
            if not self._server_phase_written:
                self._server_phase_written = True
                await self._fenced(self._machine.phase, self._holder, "start.server",
                                   answered - began)
            while self._ended is None:
                frame = await vm.recv()
                if frame is None:
                    break
                message = _json(frame)
                kind = message.get("t")
                if kind == "ready":
                    await self._fenced(self._machine.phase, self._holder, "start.load",
                                       self._clock() - answered)
                    return vm
                if kind == "boot":
                    phase = str(message.get("phase") or "")
                    if phase == "failed":
                        await self._close_vm(vm)
                        return None
                    spent()
                    if _boot_status(phase) != current:
                        current = _boot_status(phase)
                        await self._tell(_status(current))
                if spent() >= budget:
                    await self._close_vm(vm)
                    return None
            await self._sleep(RETRY_SECONDS)
        return None

    _server_phase_written = False

    @staticmethod
    async def _close_vm(vm: Socket) -> None:
        try:
            await vm.close()
        except Exception:  # noqa: BLE001
            pass

    # -- the pumps

    async def _browser_pump(self) -> str:
        while self._ended is None:
            frame = await self._browser.recv()
            if frame is None:
                return ENDED_BROWSER
            if isinstance(frame, (bytes, bytearray)):
                if self.state.utterance is not None and len(frame) <= MAX_AUDIO_FRAME:
                    await self._to_vm(bytes(frame))
                continue
            if len(frame) > MAX_TEXT_FRAME:
                continue
            try:
                message = json.loads(frame)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("t")
            if kind == "utt.start" and isinstance(message.get("seq"), int):
                self.state.utterance = message["seq"]
                self._active()
                await self._to_vm(json.dumps({"t": "utt.start", "seq": message["seq"]}))
            elif kind == "utt.end" and isinstance(message.get("seq"), int):
                self._active()
                await self._to_vm(json.dumps({"t": "utt.end", "seq": message["seq"]}))
                if self.state.utterance == message["seq"]:
                    self.state.utterance = None
                    self._spawn(self._speak_waiting())
            elif kind == "barge":
                self._active()
                await self._to_vm(json.dumps({"t": "cancel"}))
            elif kind == "point" and isinstance(message.get("anchor"), dict):
                self.state.anchor = {"visual": message.get("visual"), "anchor": message["anchor"]}
                self.state.anchor_at = self._clock()
                self._active()
            # `ping` needs no answer: the clock writes the heartbeat while the
            # socket is open, and a ping is not activity (§4.4).
        return self._ended

    async def _vm_pump(self) -> str:
        while self._ended is None:
            vm = self._vm
            frame = await vm.recv() if vm is not None else None
            if frame is None:
                if self._ended is not None:
                    break
                if not await self._recover():
                    return ENDED_ENGINE_DOWN
                continue
            if isinstance(frame, (bytes, bytearray)):
                await self._tell_bytes(bytes(frame))
                continue
            try:
                message = json.loads(frame)
            except ValueError:
                continue
            if isinstance(message, dict):
                await self._from_vm(message)
        return self._ended or ENDED_SESSION

    async def _tell_bytes(self, data: bytes) -> None:
        try:
            await self._browser.send(data)
        except Exception:  # noqa: BLE001
            pass

    async def _from_vm(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "partial":
            await self._tell({"t": "partial", "seq": message.get("seq"), "text": message.get("text", "")})
            await self._tell(_status(HEARING))
        elif kind == "final":
            text = str(message.get("text") or "")
            await self._tell({"t": "final", "seq": message.get("seq"), "text": text})
            if text.strip():
                self._active()
                self._spawn(self._final(text))
            else:
                await self._tell(_status(LISTENING))
        elif kind == "speak.start":
            say_id = str(message.get("id") or "")
            said = self.state.said.get(say_id, {})
            self.state.speaking.add(say_id)
            self._active()
            await self._tell({"t": "say.start", "id": say_id, "text": said.get("text", ""),
                              "point_at": said.get("point_at")})
            await self._tell(_status(SPEAKING))
        elif kind == "speak.end":
            say_id = str(message.get("id") or "")
            self.state.speaking.discard(say_id)
            self.state.said.pop(say_id, None)
            self._active()
            await self._tell({"t": "say.end", "id": say_id})
            if not self.state.speaking:
                await self._tell(_status(LISTENING))
        elif kind in ("timing", "metrics"):
            if self._on_frame is not None:
                try:
                    self._on_frame(message)
                except Exception as exc:  # noqa: BLE001 -- telemetry never stops speech
                    log.warning("speech telemetry frame dropped (%s)", type(exc).__name__)
        elif kind == "boot" and message.get("phase") == "failed" and self._vm is not None:
            # A server that restarted and failed its boot: the drop recovers or
            # ends as engine down.
            await self._close_vm(self._vm)

    async def _recover(self) -> bool:
        """§4.5: the speech server went; retry for CRASH_SECONDS."""
        self._vm = None
        vm = await self._connect(budget=CRASH_SECONDS, update_budget=0.0, status=RECONNECTING)
        if vm is None:
            return False
        self._vm = vm
        self.state.crashes += 1
        await self._tell(_status(READY))
        return True

    # -- turns (§3.3 steps 4-5)

    async def say(self, text: str, *, point_at: dict[str, Any] | None = None) -> str | None:
        text = text.strip()
        if not text:
            return None
        say_id = "say_" + secrets.token_hex(6)
        self.state.said[say_id] = {"text": text, "point_at": point_at}
        sent = await self._to_vm(json.dumps({"t": "speak", "id": say_id, "text": text,
                                             "voice": self._voice}))
        if not sent:
            self.state.said.pop(say_id, None)
            return None
        return say_id

    def _fresh_anchor(self) -> dict[str, Any] | None:
        if self.state.anchor is not None and self._clock() - self.state.anchor_at < ANCHOR_SECONDS:
            return self.state.anchor
        return None

    async def _final(self, text: str) -> None:
        self.state.turns_in_flight += 1
        try:
            await self._tell(_status(THINKING))
            if self._answer_readback is not None:
                try:
                    to_front = await self._answer_readback(text)
                except Exception as exc:  # noqa: BLE001 -- the words still reach the front
                    log.warning("speech read-back answer failed (%s)", type(exc).__name__)
                    to_front = True
                if not to_front:
                    await self._tell(_status(LISTENING))
                    return
            try:
                line = await self._to_thread(self._turns.turn, self._holder.session_id, text,
                                             self._fresh_anchor())
            except Exception as exc:  # noqa: BLE001 -- the class name only, never the words
                log.warning("speech turn failed (%s)", type(exc).__name__)
                await self._tell(_status(LISTENING))
                return
            if not await self.say(line.reply):
                await self._tell(_status(LISTENING))
            if line.delegation_id:
                self._spawn(self._delegated(line.delegation_id))
        finally:
            self.state.turns_in_flight -= 1
            self._active()

    async def _delegated(self, delegation_id: str) -> None:
        """The planner's line, spoken after the front's, at the next silence."""
        self.state.turns_in_flight += 1
        try:
            line = await self._to_thread(self._turns.delegation, self._holder.session_id,
                                         delegation_id, timeout=DELEGATION_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("speech delegation failed (%s)", type(exc).__name__)
            line = None
        finally:
            self.state.turns_in_flight -= 1
            self._active()
        if line:
            self.state.pending_lines.append(line)
            await self._speak_waiting()

    async def _speak_waiting(self) -> None:
        """Speak the planner lines that waited, while nobody is speaking."""
        while self.state.pending_lines and self.state.utterance is None:
            await self.say(self.state.pending_lines.pop(0))

    # -- the clock: rotation, heartbeat, idle (§3.3, §4.4)

    async def _ticker(self, began: float) -> str:
        last_beat = -sm.HEARTBEAT_SECONDS
        while self._ended is None:
            now = self._clock()
            if not self.state.rotated and now - began >= ROTATE_SECONDS:
                self.state.rotated = True
                await self._tell({"t": "rotate"})
            if now - last_beat >= sm.HEARTBEAT_SECONDS:
                last_beat = now
                if self.state.turns_in_flight or self.state.speaking or self.state.utterance is not None:
                    self._active()
                lease = await self._fenced(self._machine.beat, self._holder, socket_id=self.socket_id,
                                           activity_at=self.state.activity_at)
                if lease is None:
                    break
                cause = sm.due_cause(lease, self._machine.now())
                if cause == sm.END_IDLE:
                    await self._end(cause)
                    break
            await self._sleep(TICK_SECONDS)
        return self._ended or ENDED_SESSION

    async def _end(self, cause: str) -> None:
        if self._ended is None:
            self._ended = ENDED_SESSION
        await self._tell({"t": "ended", "cause": cause})
        if self._on_end is not None:
            await self._on_end(cause)

    # -- the whole socket

    async def run(self) -> str:
        if await self._fenced(self._machine.attach, self._holder, socket_id=self.socket_id) is None:
            await self._browser.close()
            return ENDED_STALE
        began = self._clock()
        vm = await self._connect(budget=self._budget, update_budget=sm.UPDATE_SECONDS,
                                 status=UPDATING if self._image_changed else STARTING_MACHINE)
        if self._ended == ENDED_STALE:
            await self._browser.close()
            return ENDED_STALE
        if vm is None:
            await self._engine_down()
            return self._ended or ENDED_ENGINE_DOWN
        self._vm = vm
        await self._fenced(self._machine.ready, self._holder, self._started)
        await self._tell(_status(READY))
        self._active()
        pumps = [asyncio.ensure_future(self._browser_pump()),
                 asyncio.ensure_future(self._vm_pump()),
                 asyncio.ensure_future(self._ticker(began))]
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        why = next(iter(done)).result() if done else ENDED_SESSION
        why = self._ended if self._ended in (ENDED_STALE,) else why
        for task in pumps + list(self._tasks):
            task.cancel()
        await asyncio.gather(*pumps, *self._tasks, return_exceptions=True)
        if self._vm is not None:
            await self._close_vm(self._vm)
        if why == ENDED_ENGINE_DOWN:
            await self._engine_down()
        elif why == ENDED_BROWSER:
            await self._fenced(self._machine.detach, self._holder, socket_id=self.socket_id)
        else:
            await self._browser.close()
        return why

    async def _engine_down(self) -> None:
        self._ended = self._ended or ENDED_ENGINE_DOWN
        await self._tell(_status(ENGINE_DOWN))
        if self._on_engine_down is not None:
            await self._on_engine_down(sm.FALLBACK_ENGINE_DOWN)
        await self._browser.close()


async def wait_for_reconnect(machine: sm.SpeechMachine, holder: sm.Holder, *,
                             on_end: Callable[[str], Awaitable[None]],
                             sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                             to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread) -> bool:
    """§4.5: a closed tab is given TAB_CLOSED_SECONDS to come back (a reload,
    a rotation through any swarm-api instance). True when it did not, and
    the session was ended `tab_closed`."""
    await sleep(sm.TAB_CLOSED_SECONDS)
    lease = await to_thread(machine.read)
    if lease is None or lease.get("generation") != holder.generation \
            or lease.get("session_id") != holder.session_id:
        return False
    if sm.due_cause(lease, machine.now()) != sm.END_TAB_CLOSED:
        return False
    await on_end(sm.END_TAB_CLOSED)
    return True
