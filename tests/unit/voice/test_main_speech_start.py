"""swarm-voice's speech start (docs/design/voice-it-own-speech.md §4.1, §10.2,
§14 row L6; apps/swarm-voice/swarm_voice/main.py).

A `speech` start has no SDP, like a text start, but its conversation is the
fast Claude front (`claudefront.FrontSessions`), started on the conversation
model Settings name, and never textchat.py's: a speech start never builds a
text session, and a speech session's turn never reaches textchat. A front
refusal is answered with a fixed code, which swarm-api reads as the text
fallback. `/turn` routes by the session's mode; `/readback` (readback.py) and
`/delegations/{id}` (claudefront.py) are mounted beside `/internal/sessions`.

Every collaborator is a fake with the real one's shape: no network, no
credential, no Claude Code child.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

_APP = Path(__file__).resolve().parents[3] / "apps" / "swarm-voice"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from swarm_voice import claudefront  # noqa: E402
from swarm_voice import main as voice_main  # noqa: E402
from swarm_voice import plannerlease  # noqa: E402
from swarm_voice.textchat import TextSessionEnded  # noqa: E402

PERSON = "owner@example.com"
CALLER = "swarm-api@example-project.iam.gserviceaccount.com"
VOICE_ID = "vs_" + "c0ffee1234"
AUTH = {"Authorization": "Bearer caller-token"}


async def _caller_ok(token: str) -> str:
    if token != "caller-token":
        raise PermissionError("not swarm-api")
    return CALLER


class FakeLive:
    """live.Sessions' shape, holding nothing."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def get(self, voice_id: str) -> Any:
        return None

    async def start(self, voice_id: str, **_kwargs: Any) -> Any:
        self.started.append(voice_id)
        raise AssertionError("no Live session in these tests")

    async def end(self, voice_id: str) -> Any:
        return None

    async def end_all(self) -> None:
        return None

    def counts(self) -> dict[str, int]:
        return {"sessions": 0, "attached": 0}


class FakeTexts:
    """textchat.TextSessions' shape: records every call, so a test can say
    textchat was never reached."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.turns: list[str] = []
        self.held: set[str] = set()

    def get(self, voice_id: str) -> Any:
        return object() if voice_id in self.held else None

    async def start(self, voice_id: str, *, person: str, grant: Any = None,
                    exists: Any = None, planner_model: str | None = None) -> Any:
        self.starts.append(voice_id)
        if exists is not None and exists(voice_id):
            raise voice_main.SessionExists(voice_id)
        self.held.add(voice_id)
        return type("Started", (), {"voice_id": voice_id})()

    async def turn(self, voice_id: str, text: str) -> Any:
        self.turns.append(voice_id)
        if voice_id not in self.held:
            raise TextSessionEnded(voice_id)
        return type("Answer", (), {"turn_id": "vt_1", "reply": "text reply", "cards": []})()

    async def end(self, voice_id: str) -> Any:
        self.held.discard(voice_id)
        return None

    async def end_all(self) -> None:
        self.held.clear()

    def counts(self) -> dict[str, int]:
        return {"text_sessions": len(self.held)}


class FakeFronts:
    """claudefront.FrontSessions' shape."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.starts: list[dict[str, Any]] = []
        self.turns: list[tuple[str, str]] = []
        self.ended: list[tuple[str, str]] = []
        self.held: set[str] = set()
        self.delegations: dict[str, claudefront.Delegation] = {}

    def get(self, voice_id: str) -> Any:
        return object() if voice_id in self.held else None

    async def start(self, voice_id: str, *, person: str, grant: Any = None, exists: Any = None,
                    planner_model: str | None = None, conversation_model: str | None = None) -> Any:
        self.starts.append({"voice_id": voice_id, "person": person, "planner_model": planner_model,
                            "conversation_model": conversation_model})
        if exists is not None and exists(voice_id):
            raise claudefront.SessionExists(voice_id)
        if self.refuse is not None:
            raise claudefront.FrontRefused(self.refuse)
        self.held.add(voice_id)
        return type("Front", (), {"voice_id": voice_id})()

    async def turn(self, voice_id: str, text: str) -> claudefront.FrontAnswer:
        self.turns.append((voice_id, text))
        if voice_id not in self.held:
            raise claudefront.FrontSessionEnded(voice_id)
        delegation = claudefront.Delegation(delegation_id="vd_1", voice_id=voice_id, request=text)
        self.delegations[delegation.delegation_id] = delegation
        return claudefront.FrontAnswer("vf_1", "Looking at that now.", "vd_1")

    def delegation(self, delegation_id: str) -> claudefront.Delegation | None:
        return self.delegations.get(delegation_id)

    async def end(self, voice_id: str, reason: str = "client_close") -> Any:
        if voice_id not in self.held:
            return None
        self.held.discard(voice_id)
        self.ended.append((voice_id, reason))
        return type("Ended", (), {"usage_seconds": 3, "reason": reason})()

    async def end_all(self) -> None:
        self.held.clear()

    def counts(self) -> dict[str, int]:
        return {"front_sessions": len(self.held), "front_delegations": 0}


def run(scenario, *, fronts: FakeFronts | None = None):
    async def main():
        texts, fronts_ = FakeTexts(), fronts or FakeFronts()
        app = voice_main.create_app(sessions=FakeLive(), verify_caller=_caller_ok, texts=texts, fronts=fronts_)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice")
        try:
            return await asyncio.wait_for(scenario(client, texts, fronts_), 20)
        finally:
            await client.aclose()

    return asyncio.run(main())


def _speech_start(**extra: Any) -> dict[str, Any]:
    return {"voice_session_id": VOICE_ID, "person": PERSON, "mode": "speech", **extra}


def test_a_speech_start_starts_the_front_and_never_builds_textchats_session():
    model = claudefront.FRONT_MODELS[-1]

    async def scenario(client, texts, fronts):
        r = await client.post("/internal/sessions", json=_speech_start(conversation_model=model), headers=AUTH)
        assert r.status_code == 201, r.text
        assert r.json() == {"voice_session_id": VOICE_ID, "mode": "speech"}
        assert texts.starts == [] and texts.get(VOICE_ID) is None
        (start,) = fronts.starts
        assert start["person"] == PERSON and start["conversation_model"] == model
        # The same session cannot then start as text while its front lives.
        again = await client.post("/internal/sessions", json={**_speech_start(), "mode": "text"}, headers=AUTH)
        assert again.status_code == 409

    run(scenario)


def test_a_speech_start_takes_no_offer():
    async def scenario(client, texts, fronts):
        r = await client.post("/internal/sessions", json=_speech_start(offer="v=0\r\no=- 1 2 IN IP4 x\r\n"),
                              headers=AUTH)
        assert r.status_code == 422
        assert fronts.starts == [] and texts.starts == []

    run(scenario)


def test_a_front_refusal_is_the_fixed_code_swarm_api_falls_back_on():
    async def scenario(client, texts, fronts):
        r = await client.post("/internal/sessions", json=_speech_start(), headers=AUTH)
        assert r.status_code == 409
        assert r.json()["code"] == voice_main.FRONT_REFUSED == "voice_front_refused"
        assert texts.starts == [] and fronts.held == set()

    run(scenario, fronts=FakeFronts(refuse=plannerlease.NO_ACCOUNT_AVAILABLE))


def test_a_speech_sessions_turn_never_reaches_textchat_and_carries_the_delegation():
    async def scenario(client, texts, fronts):
        assert (await client.post("/internal/sessions", json=_speech_start(), headers=AUTH)).status_code == 201
        r = await client.post(f"/internal/sessions/{VOICE_ID}/turns", json={"text": "plan it"}, headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json() == {"turn_id": "vf_1", "reply": "Looking at that now.", "cards": [],
                            "delegation_id": "vd_1"}
        assert fronts.turns == [(VOICE_ID, "plan it")]
        assert texts.turns == []

    run(scenario)


def test_a_text_sessions_turn_still_reaches_textchat():
    async def scenario(client, texts, fronts):
        start = {**_speech_start(), "mode": "text"}
        assert (await client.post("/internal/sessions", json=start, headers=AUTH)).status_code == 201
        r = await client.post(f"/internal/sessions/{VOICE_ID}/turns", json={"text": "hello"}, headers=AUTH)
        assert r.status_code == 200 and r.json()["reply"] == "text reply"
        assert texts.turns == [VOICE_ID] and fronts.turns == []

    run(scenario)


def test_ending_a_speech_session_ends_its_front():
    async def scenario(client, texts, fronts):
        await client.post("/internal/sessions", json=_speech_start(), headers=AUTH)
        r = await client.post(f"/internal/sessions/{VOICE_ID}/end", headers=AUTH)
        assert r.status_code == 200 and r.json()["ended"] is True
        assert fronts.ended == [(VOICE_ID, "client_close")]
        # Then the same id may start as text: swarm-api's fallback after the machine failed.
        start = {**_speech_start(), "mode": "text"}
        assert (await client.post("/internal/sessions", json=start, headers=AUTH)).status_code == 201

    run(scenario)


def test_delegations_and_readback_are_mounted_behind_the_caller_check():
    async def scenario(client, texts, fronts):
        await client.post("/internal/sessions", json=_speech_start(), headers=AUTH)
        await client.post(f"/internal/sessions/{VOICE_ID}/turns", json={"text": "plan it"}, headers=AUTH)
        assert (await client.get("/delegations/vd_1")).status_code == 401
        got = await client.get("/delegations/vd_1", headers=AUTH)
        assert got.status_code == 200 and got.json()["voice_session_id"] == VOICE_ID
        assert (await client.get("/delegations/vd_none", headers=AUTH)).status_code == 404
        readback = f"/internal/sessions/{VOICE_ID}/readback"
        assert (await client.post(readback, json={"readback_id": "vr_" + "0" * 32})).status_code == 401
        held = await client.post(readback, json={"readback_id": "vr_" + "0" * 32}, headers=AUTH)
        assert held.status_code == 404

    run(scenario)


def test_healthz_counts_the_fronts():
    async def scenario(client, texts, fronts):
        await client.post("/internal/sessions", json=_speech_start(), headers=AUTH)
        assert (await client.get("/healthz")).json()["front_sessions"] == 1

    run(scenario)


def test_the_deployments_fronts_hold_their_accounts_as_the_front():
    class Live:
        _leases = object()
        _planner = None

    fronts = voice_main.build_fronts(Live())
    assert isinstance(fronts, claudefront.FrontSessions)
    assert fronts._accounts.holder == plannerlease.FRONT_HOLDER  # noqa: SLF001
