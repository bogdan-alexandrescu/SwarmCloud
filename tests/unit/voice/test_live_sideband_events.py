"""The GPT-Live sideband does not open with the event its reference names.

Live QA, 9 November 2026: on a funded developer API key the session create
answered 201 and the sideband's first event was not `session.updated`, so an
attach that read exactly one event refused a good session. This holds that:

* an attach waits, within ATTACH_SECONDS, for `session.updated` or
  `session.created`, skipping any other non-error event and logging the skipped
  TYPES once, never their content;
* an `error` event is still refused at once, and nothing confirming within the
  bound is refused with the types seen;
* a re-attach after a drop and the close handshake tolerate the same
  interleaved events, and a re-attach to a session that already ended is not
  sent a second close.

The sideband is a fake socket in place of `websockets`' connect; every token is
built at runtime with `secrets.token_hex`; nothing needs a cloud credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedError

_APP = Path(__file__).resolve().parents[3] / "apps" / "swarm-voice"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from swarm_voice import credential as cred  # noqa: E402
from swarm_voice import live  # noqa: E402

#: Transcript text an interleaved event carries: never logged.
SPOKEN = "my secret plan is " + secrets.token_hex(6)


class _State:
    name = "OPEN"


class FakeSocket:
    """A sideband: `script` is what the server sends, in order; past its end
    `recv` waits (or the socket drops, with `drop_at_end`)."""

    def __init__(self, script: list, *, drop_at_end: bool = False, answer_close: list | None = None) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        for event in script:
            self.inbox.put_nowait(event)
        self.drop_at_end = drop_at_end
        #: Sent in answer to a `session.close`, in order.
        self.answer_close = answer_close
        self.sent: list[dict] = []
        self.closed = False
        self.state = _State()

    async def recv(self):
        if self.inbox.empty() and self.drop_at_end:
            raise ConnectionClosedError(None, None)
        event = await self.inbox.get()
        if event is None:
            raise ConnectionClosedError(None, None)
        return event if isinstance(event, str) else json.dumps(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            return await self.recv()
        except ConnectionClosedError:
            if self.closed:
                raise StopAsyncIteration from None
            raise

    async def send(self, raw: str) -> None:
        if self.closed:
            raise ConnectionClosedError(None, None)
        event = json.loads(raw)
        self.sent.append(event)
        if event.get("type") == "session.close" and self.answer_close is not None:
            for answer in self.answer_close:
                self.inbox.put_nowait(answer)

    async def close(self) -> None:
        self.closed = True
        self.state.name = "CLOSED"
        self.inbox.put_nowait(None)


class Wire:
    """Hands out the next fake socket on each connect."""

    def __init__(self, sockets: list[FakeSocket]) -> None:
        self.sockets = list(sockets)
        self.connects = 0
        self.max_sizes: list[int] = []

    async def connect(self, url, **kwargs):
        self.connects += 1
        self.max_sizes.append(kwargs.get("max_size"))
        return self.sockets.pop(0)


class _OneBearer:
    async def bearer(self) -> cred.Bearer:
        return cred.Bearer("at_" + secrets.token_hex(8))

    async def refused(self) -> None:
        return None


class _Lease(_OneBearer):
    def start_renewing(self) -> None:
        return None

    async def release(self) -> None:
        return None


def _api(monkeypatch, sockets: list[FakeSocket]) -> tuple[live.LiveApi, Wire]:
    wire = Wire(sockets)
    monkeypatch.setattr(live, "ws_connect", wire.connect)
    return live.LiveApi(http=None), wire  # type: ignore[arg-type]


def _created() -> dict:
    return {"type": "session.created", "session": {"id": "live_" + secrets.token_hex(4)}}


def _updated() -> dict:
    return {"type": "session.updated", "session": {"id": "live_" + secrets.token_hex(4)}}


def _rate_limits() -> dict:
    return {"type": "rate_limits.updated", "rate_limits": [{"name": "requests", "remaining": 9}]}


def _spoken() -> dict:
    return {"type": "session.input_transcript.done", "transcript": SPOKEN}


def _closed(seconds: int = 42) -> dict:
    return {"type": "session.closed", "reason": "client_close", "usage": {"seconds": seconds}}


def _attach(api: live.LiveApi, held: list | None = None):
    live_id = "live_" + secrets.token_hex(6)
    if held is None:
        return asyncio.run(api.attach(_OneBearer(), live_id))
    return asyncio.run(api.attach(_OneBearer(), live_id, held))


def _logged(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- attach


def test_session_created_then_session_updated_attaches(monkeypatch):
    socket = FakeSocket([_created(), _updated()])
    api, wire = _api(monkeypatch, [socket])
    assert _attach(api) is socket
    assert not socket.closed
    # The socket size limit stands.
    assert wire.max_sizes == [4 * 1024 * 1024]


def test_session_created_alone_attaches(monkeypatch):
    socket = FakeSocket([_created()])
    api, _ = _api(monkeypatch, [socket])
    assert _attach(api) is socket and not socket.closed


def test_a_rate_limits_event_before_session_updated_attaches_and_logs_its_type_only(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="swarm_voice")
    socket = FakeSocket([_rate_limits(), _spoken(), _updated()])
    api, _ = _api(monkeypatch, [socket])
    assert _attach(api) is socket and not socket.closed
    lines = [r.getMessage() for r in caplog.records if "before session.updated" in r.getMessage()]
    # One line for the attach, naming each skipped type in order.
    assert lines == ["the Live sideband sent rate_limits.updated, session.input_transcript.done "
                     "before session.updated"]
    # Never their content.
    assert SPOKEN not in _logged(caplog) and "remaining" not in _logged(caplog)


def test_an_error_first_is_refused_at_once(monkeypatch):
    monkeypatch.setattr(live, "ATTACH_SECONDS", 5.0)
    quoted = "sk-" + secrets.token_hex(24)
    error = {"type": "error", "error": {"message": f"echo {quoted}", "code": "plan_not_allowed",
                                        "type": "invalid_request_error"}}
    socket = FakeSocket([error, _updated()])
    api, _ = _api(monkeypatch, [socket])

    async def timed():
        started = asyncio.get_running_loop().time()
        with pytest.raises(live.LiveRefused) as raised:
            await api.attach(_OneBearer(), "live_" + secrets.token_hex(6))
        return raised.value, asyncio.get_running_loop().time() - started

    refused, took = asyncio.run(timed())
    assert (refused.status, refused.code, refused.type) == (None, "plan_not_allowed", "invalid_request_error")
    assert quoted not in str(refused)
    assert took < 1.0 and socket.closed


def test_an_error_after_skipped_events_is_still_refused(monkeypatch):
    error = {"type": "error", "error": {"code": "session_expired", "type": "invalid_request_error"}}
    socket = FakeSocket([_rate_limits(), error, _updated()])
    api, _ = _api(monkeypatch, [socket])
    with pytest.raises(live.LiveRefused) as raised:
        _attach(api)
    assert raised.value.code == "session_expired" and socket.closed


def test_nothing_confirming_within_the_bound_is_refused_with_the_types_seen(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="swarm_voice")
    monkeypatch.setattr(live, "ATTACH_SECONDS", 0.2)
    socket = FakeSocket([_rate_limits(), _spoken(), "not json", {"type": "Has Spaces " + SPOKEN}])
    api, _ = _api(monkeypatch, [socket])
    with pytest.raises(live.LiveError) as raised:
        _attach(api)
    assert not isinstance(raised.value, live.LiveRefused)
    message = str(raised.value)
    assert ("saw rate_limits.updated, session.input_transcript.done, (unreadable), (unnamed)"
            in message), message
    assert "TimeoutError" in message
    assert SPOKEN not in message and SPOKEN not in _logged(caplog)
    assert socket.closed


def test_a_socket_that_closes_before_confirming_is_refused_with_the_types_seen(monkeypatch):
    socket = FakeSocket([_rate_limits()], drop_at_end=True)
    api, _ = _api(monkeypatch, [socket])
    with pytest.raises(live.LiveError) as raised:
        _attach(api)
    assert "saw rate_limits.updated" in str(raised.value)
    assert "ConnectionClosedError" in str(raised.value)


def test_the_skipped_events_are_held_for_the_reader(monkeypatch):
    rate, spoken, created = _rate_limits(), _spoken(), _created()
    socket = FakeSocket([rate, spoken, created])
    api, _ = _api(monkeypatch, [socket])
    held: list = []
    assert _attach(api, held) is socket
    assert held == [rate, spoken, created]


# ---------------------------------------------------------------- the session's sideband


def _session(api: live.LiveApi, events: list, closed: list) -> live.LiveSession:
    async def sink(voice_id, event):
        events.append(event)

    return live.LiveSession("vs_" + secrets.token_hex(5), "live_" + secrets.token_hex(6), _Lease(), api,
                            on_event=sink, on_closed=closed.append)


async def _detached(session: live.LiveSession) -> None:
    for _ in range(200):
        if not session.attached:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the sideband never detached")


def test_a_reattach_after_a_drop_tolerates_interleaved_events_and_keeps_them(monkeypatch):
    first = FakeSocket([_created(), _updated()], drop_at_end=True)
    spoken = _spoken()
    second = FakeSocket([_rate_limits(), spoken, _updated()], answer_close=[_rate_limits(), _closed()])
    api, wire = _api(monkeypatch, [first, second])
    events: list = []
    closed: list = []

    async def scenario():
        session = _session(api, events, closed)
        await session.attach()
        await _detached(session)
        await session.end(grace=2.0)
        return session

    session = asyncio.run(scenario())
    assert wire.connects == 2
    assert second.sent == [{"type": "session.close"}]
    assert session.ended and session.reason == "client_close" and session.usage_seconds == 42
    assert closed == [session]
    # The final spoken while the re-attach waited still reached the sink and the planner's window.
    assert spoken in events
    assert session.transcript == [("person", SPOKEN)]


def test_the_close_handshake_waits_for_session_closed_past_interleaved_events(monkeypatch):
    socket = FakeSocket([_created()], answer_close=[_rate_limits(), _spoken(), _closed(7)])
    api, _ = _api(monkeypatch, [socket])
    closed: list = []

    async def scenario():
        session = _session(api, [], closed)
        await session.attach()
        await session.end(grace=2.0)
        return session

    session = asyncio.run(scenario())
    assert session.reason == "client_close" and session.usage_seconds == 7
    assert closed == [session]


def test_a_reattach_that_finds_the_session_closed_sends_no_second_close(monkeypatch):
    first = FakeSocket([_updated()], drop_at_end=True)
    second = FakeSocket([_rate_limits(), _closed(9)])
    api, _ = _api(monkeypatch, [first, second])
    closed: list = []

    async def scenario():
        session = _session(api, [], closed)
        await session.attach()
        await _detached(session)
        await session.end(grace=2.0)
        return session

    session = asyncio.run(scenario())
    assert second.sent == []
    assert session.ended and session.reason == "client_close" and session.usage_seconds == 9
    assert closed == [session]
