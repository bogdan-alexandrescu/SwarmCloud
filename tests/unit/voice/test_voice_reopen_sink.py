"""A spent Live key's next tier reaches swarm-api through the session's event
sink (swarm_voice/main.py THE REOPEN REACHES THE CONSOLE; live.py THE NEXT TIER).

`build_sessions` wires `ReopenSink` as the Live sessions' sink when
SWARM_API_URL is set; the sink posts `{tier}` to swarm-api's reopen route with
this service's ID token and the session's grant, the planner's own path, and
drops every other event (the sideband's transcripts) unread. Every token is
built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx
import pytest

_APP = Path(__file__).resolve().parents[3] / "apps" / "swarm-voice"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from swarm_voice import accountlease as al  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice import main as voice_main  # noqa: E402
from swarm_voice.grant import GRANT_HEADER, SESSION_HEADER, SessionGrant  # noqa: E402

from test_voice_fallback_order import PERSON, Account, FakeLiveApi, _broker, _offer, _until  # noqa: E402

API_URL = "http://swarm-api.swarm-system.svc.cluster.local:8080"


@pytest.fixture(autouse=True)
def broker_url(monkeypatch):
    monkeypatch.setenv("QUOTA_BROKER_URL", "https://swarm-quota-broker.test")
    monkeypatch.setenv("PROJECT_ID", "swarm-test")


def test_the_session_builder_wires_the_reopen_sink_to_its_sessions(monkeypatch):
    monkeypatch.setenv("SWARM_API_URL", API_URL)

    async def scenario():
        async with httpx.AsyncClient() as http:
            sessions = voice_main.build_sessions(http)
            sink = sessions._on_event  # noqa: SLF001
            assert isinstance(sink, voice_main.ReopenSink)
            assert sink._sessions is sessions and sink._base == API_URL  # noqa: SLF001

    asyncio.run(scenario())


def test_without_swarm_apis_address_no_sink_is_wired(monkeypatch):
    monkeypatch.delenv("SWARM_API_URL", raising=False)

    async def scenario():
        async with httpx.AsyncClient() as http:
            assert voice_main.build_sessions(http)._on_event is None  # noqa: SLF001

    asyncio.run(scenario())


def test_a_spent_key_tells_swarm_api_its_next_tier_through_the_grant_and_nothing_else():
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, _, broker_http, voice = _broker({"live": [[shared], [dedicated]], "text": [[]]})
    api = FakeLiveApi()
    voice_id = "vs_" + secrets.token_hex(16)
    grant = SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id=voice_id,
                         value=secrets.token_urlsafe(32))
    id_token = "id-" + secrets.token_hex(12)
    seen: list[httpx.Request] = []

    def swarm_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"session_id": voice_id, "tier": al.TIER_VOICE_KEY})

    async def tokens() -> str:
        return id_token

    async def scenario():
        api_http = httpx.AsyncClient(transport=httpx.MockTransport(swarm_api))
        sink = voice_main.ReopenSink(api_http, api_base=API_URL, id_tokens=tokens)
        sessions = live.Sessions(api, leases=voice, on_event=sink, close_grace=1.0)
        sink.bind(sessions)
        first = await sessions.start(voice_id, person=PERSON, offer=_offer(), grant=grant)
        socket = api.sockets[first.live_id]
        # A transcript on the sideband goes through the sink and never leaves.
        socket.push({"type": "conversation.item.input_audio_transcription.completed",
                     "item_id": "i1", "transcript": "say nothing of this"})
        shared.limited = True
        socket.push({"type": "error", "error": {"code": "insufficient_quota", "type": "insufficient_quota"}})
        await _until(lambda: seen)
        assert first.reopen_pending
        await sessions.end_all()
        await api_http.aclose()
        await broker_http.aclose()

    asyncio.run(scenario())
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{API_URL}/v1/voice/sessions/{voice_id}/reopen"
    assert json.loads(request.content) == {"tier": al.TIER_VOICE_KEY}
    assert request.headers["authorization"] == f"Bearer {id_token}"
    assert request.headers[SESSION_HEADER] == f"{grant.project_id}/{voice_id}"
    assert request.headers[GRANT_HEADER] == grant.headers()[GRANT_HEADER]
    # A tier's name, never a key the broker minted.
    for minted in broker.minted:
        assert minted not in request.content.decode()


def test_a_session_with_no_grant_tells_nothing_and_a_refusal_never_fails_the_move(caplog):
    seen: list[httpx.Request] = []

    def swarm_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, json={"code": "forbidden"})

    async def tokens() -> str:
        return "id-" + secrets.token_hex(12)

    class Held:
        def __init__(self, grant):
            self.grant = grant

    class FakeSessions:
        def __init__(self):
            self.live: dict[str, Held] = {}
            self._awaiting: dict[str, Held] = {}

        def get(self, voice_id):
            return self.live.get(voice_id)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(swarm_api)) as http:
            sink = voice_main.ReopenSink(http, api_base=API_URL, id_tokens=tokens)
            held = FakeSessions()
            sink.bind(held)  # type: ignore[arg-type]
            held.live["vs_none"] = Held(None)
            await sink("vs_none", {"type": live.REOPEN_EVENT, "tier": al.TIER_KEY})
            assert seen == []
            voice_id = "vs_" + secrets.token_hex(16)
            held._awaiting[voice_id] = Held(SessionGrant(project_id="vp_" + secrets.token_hex(16),
                                                         session_id=voice_id, value=secrets.token_urlsafe(32)))
            await sink(voice_id, {"type": live.REOPEN_EVENT, "tier": al.TIER_KEY})
            assert len(seen) == 1

    asyncio.run(scenario())
    assert "was not told" in caplog.text


# ---- the voice's state reaches swarm-api (owner, 12 November 2026) -----------


class _Held:
    def __init__(self, grant):
        self.grant = grant


class _HeldSessions:
    """The sink's view of Sessions: a live session, or a spent one awaiting its reopen."""

    def __init__(self):
        self.live: dict[str, _Held] = {}
        self._awaiting: dict[str, _Held] = {}

    def get(self, voice_id):
        return self.live.get(voice_id)


def _state_world(answers):
    """A sink over a fake swarm-api answering each post with the next status in
    `answers` (then 200); every request is kept."""
    voice_id = "vs_" + secrets.token_hex(16)
    grant = SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id=voice_id,
                         value=secrets.token_urlsafe(32))
    id_token = "id-" + secrets.token_hex(12)
    seen: list[httpx.Request] = []
    statuses = list(answers)

    def swarm_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = statuses.pop(0) if statuses else 200
        return httpx.Response(status, json={"session_id": voice_id})

    async def tokens() -> str:
        return id_token

    http = httpx.AsyncClient(transport=httpx.MockTransport(swarm_api))
    sink = voice_main.ReopenSink(http, api_base=API_URL, id_tokens=tokens)
    held = _HeldSessions()
    held.live[voice_id] = _Held(grant)
    sink.bind(held)  # type: ignore[arg-type]
    return sink, http, voice_id, grant, id_token, seen


def _bodies(seen):
    return [json.loads(request.content) for request in seen]


def test_a_pause_posts_one_paused_report_with_its_cause_through_the_grant():
    sink, http, voice_id, grant, id_token, seen = _state_world([])

    async def scenario():
        await sink(voice_id, {"type": live.SESSION_STATE, "state": live.PAUSED, "cause": "quiet",
                              "paused_at": "2026-11-12T02:00:00Z", "usage_seconds": 41.0})
        await sink.drain()
        await http.aclose()

    asyncio.run(scenario())
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{API_URL}/v1/voice/sessions/{voice_id}/reopen"
    # The state and its cause, nothing else of the event.
    assert json.loads(request.content) == {"state": live.PAUSED, "cause": "quiet"}
    assert request.headers["authorization"] == f"Bearer {id_token}"
    assert request.headers[SESSION_HEADER] == f"{grant.project_id}/{voice_id}"
    assert request.headers[GRANT_HEADER] == grant.headers()[GRANT_HEADER]


def test_a_paused_event_with_no_cause_posts_the_state_alone():
    sink, http, voice_id, _, _, seen = _state_world([])

    async def scenario():
        await sink(voice_id, {"type": live.SESSION_STATE, "state": live.PAUSED,
                              "paused_at": "2026-11-12T02:00:00Z", "usage_seconds": 41.0})
        await sink.drain()
        await http.aclose()

    asyncio.run(scenario())
    assert _bodies(seen) == [{"state": live.PAUSED}]


def test_a_wake_posts_waking_then_listening_in_order():
    sink, http, voice_id, _, _, seen = _state_world([])

    async def scenario():
        await sink(voice_id, {"type": live.SESSION_STATE, "state": live.WAKING, "cause": "result"})
        await sink(voice_id, {"type": live.SESSION_STATE, "state": live.LISTENING, "cause": "result",
                              "woke_at": "2026-11-12T02:01:00Z"})
        await sink.drain()
        await http.aclose()

    asyncio.run(scenario())
    assert _bodies(seen) == [{"state": live.WAKING, "cause": "result"},
                             {"state": live.LISTENING, "cause": "result"}]


def test_a_state_report_never_holds_the_voice_while_swarm_api_is_slow():
    voice_id = "vs_" + secrets.token_hex(16)
    grant = SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id=voice_id,
                         value=secrets.token_urlsafe(32))
    seen: list[str] = []

    async def scenario():
        gate = asyncio.Event()

        async def swarm_api(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            seen.append(json.loads(request.content)["state"])
            return httpx.Response(200, json={})

        async def tokens() -> str:
            return "id-" + secrets.token_hex(12)

        async with httpx.AsyncClient(transport=httpx.MockTransport(swarm_api)) as http:
            sink = voice_main.ReopenSink(http, api_base=API_URL, id_tokens=tokens)
            held = _HeldSessions()
            held.live[voice_id] = _Held(grant)
            sink.bind(held)  # type: ignore[arg-type]
            # Both return at once, while swarm-api has answered nothing.
            await asyncio.wait_for(sink(voice_id, {"type": live.SESSION_STATE, "state": live.WAKING,
                                                   "cause": "speech"}), 0.5)
            await asyncio.wait_for(sink(voice_id, {"type": live.SESSION_STATE, "state": live.LISTENING,
                                                   "cause": "speech"}), 0.5)
            assert seen == []
            gate.set()
            await sink.drain()

    asyncio.run(scenario())
    assert seen == [live.WAKING, live.LISTENING]


def test_the_tier_reopen_still_posts_its_tier_alone_and_inline():
    sink, http, voice_id, _, _, seen = _state_world([])

    async def scenario():
        await sink(voice_id, {"type": live.REOPEN_EVENT, "tier": al.TIER_VOICE_KEY})
        # Posted before the sink returns, as before: nothing to drain.
        assert len(seen) == 1
        await http.aclose()

    asyncio.run(scenario())
    assert _bodies(seen) == [{"tier": al.TIER_VOICE_KEY}]


def test_a_failed_state_post_is_retried_once():
    sink, http, voice_id, _, _, seen = _state_world([503])

    async def scenario():
        await sink(voice_id, {"type": live.SESSION_STATE, "state": live.PAUSED, "cause": "quiet"})
        await sink.drain()
        await http.aclose()

    asyncio.run(scenario())
    assert _bodies(seen) == [{"state": live.PAUSED, "cause": "quiet"}] * 2


def test_a_state_post_failing_twice_is_dropped_with_a_log_line_that_carries_no_content(caplog):
    sink, http, voice_id, grant, id_token, seen = _state_world([503, 503, 200])
    caplog.set_level("INFO", logger="swarm_voice")

    async def scenario():
        await sink(voice_id, {"type": live.SESSION_STATE, "state": live.WAKING, "cause": "speech",
                              "transcript": "say nothing of this"})
        await sink.drain()
        await http.aclose()

    asyncio.run(scenario())
    # Two tries, then dropped: the third answer is never asked for.
    assert len(seen) == 2
    assert all(json.loads(request.content) == {"state": live.WAKING, "cause": "speech"} for request in seen)
    dropped = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert len(dropped) == 1
    assert voice_id in dropped[0] and live.WAKING in dropped[0] and "503" in dropped[0]
    for secret in (grant.headers()[GRANT_HEADER], id_token, "say nothing of this"):
        assert secret not in caplog.text


def test_a_state_event_with_no_grant_or_an_unknown_state_posts_nothing():
    sink, http, voice_id, _, _, seen = _state_world([])

    async def scenario():
        await sink("vs_unknown", {"type": live.SESSION_STATE, "state": live.PAUSED})
        await sink(voice_id, {"type": live.SESSION_STATE, "state": "thinking"})
        await sink.drain()
        await http.aclose()

    asyncio.run(scenario())
    assert seen == []


def test_a_real_pause_and_wake_reach_swarm_api_through_the_sink():
    """End to end over live.Sessions: the pause posts `paused` (live.py's
    paused event names no cause), and a reopen by speech posts `listening`
    with its cause."""
    from test_live_pause import World, _paused  # noqa: E402

    world = World()
    seen: list[dict] = []

    def swarm_api(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async def tokens() -> str:
        return "id-" + secrets.token_hex(12)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(swarm_api)) as http:
            sink = voice_main.ReopenSink(http, api_base=API_URL, id_tokens=tokens)
            sink.bind(world.sessions)
            world.sessions._on_event = sink  # noqa: SLF001
            session, voice_id = await _paused(world)
            await sink.drain()
            assert seen == [{"state": live.PAUSED}]
            await world.sessions.reopen(voice_id, offer=_offer(), cause="speech")
            await sink.drain()
            await world.sessions.end_all()

    asyncio.run(scenario())
    assert seen == [{"state": live.PAUSED}, {"state": live.LISTENING, "cause": "speech"}]
