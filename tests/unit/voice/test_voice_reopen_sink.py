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
