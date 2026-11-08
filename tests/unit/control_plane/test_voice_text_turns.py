"""Voice it's modes and the text turns route (owner, 4 November 2026: "both
paths"; docs/design/voice-it.md §3.2; docs/design/voice-it-own-speech.md §10).

`POST /v1/voice/sessions` takes its engine from Voice it › Settings alone.
GPT-Live is `live` when the broker's Voice it pool holds an OpenAI API key
(today's path, its SDP answer as today) and refused with a fixed code when it
holds none. Text mode is the fallback of own speech that cannot start (here: no
speech machine deployed), with no SDP, and the browser posts each spoken turn
to `POST /v1/voice/sessions/{id}/turns`. swarm-voice and the broker are fakes
on `app.state`; no network.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.voice import sessions as vs
from swarm_api.voice import settings as vsettings
from swarm_api.voice import speech_catalogue as cat

from .conftest import auth_header
from .test_voice_sessions import ANSWER, OFFER, FakeVoice, _client, _end, _sessions


class FakePool:
    """The broker's `GET /v1/voice/pool`, as `BrokerClient.voice_pool` answers it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.asked: list[str] = []

    def voice_pool(self, person: str) -> dict:
        self.asked.append(person)
        account = None if self.reason else {"label": "key", "display_name": None, "provider": "openai-api"}
        return {"account": account, "reason": self.reason}


class BrokenPool:
    def voice_pool(self, person: str) -> dict:
        raise OSError("broker down")


class TextVoice(FakeVoice):
    """FakeVoice that also answers text starts and turns."""

    def __init__(self) -> None:
        super().__init__()
        self.turns: list[tuple[str, str]] = []
        self.turn_error: Exception | None = None

    def start(self, call: vs.StartCall) -> vs.StartAnswer:
        if call.mode == vs.MODE_TEXT:
            self.starts.append(call)
            return vs.StartAnswer(answer="", live_session_id="", mode=vs.MODE_TEXT)
        return super().start(call)

    def turn(self, session_id: str, text: str) -> vs.TurnAnswer:
        self.turns.append((session_id, text))
        if self.turn_error is not None:
            raise self.turn_error
        return vs.TurnAnswer(turn_id="vt_" + secrets.token_hex(6), reply="Three steps are on screen.",
                             cards=[{"kind": "plan", "card_id": "card_1", "title": "Rollout"}])


def _app(db, tokens, group_map, objects, voice, pool) -> TestClient:
    client = _client(db, tokens, group_map, objects, voice, voice_url="http://voice.internal:8080",
                     voice_people=("alice@example.com", "bob@example.com"))
    client.app.state.account_pool = pool
    return client


def _engine(db, engine: str) -> None:
    db.docs[f"{vsettings.COLLECTION}/{vsettings.DOCUMENT}"] = {"engine": engine}


def _text_app(db, tokens, group_map, objects, voice, pool) -> TestClient:
    """Settings' own speech on a deployment with no speech machine: every
    start falls back to text mode."""
    client = _app(db, tokens, group_map, objects, voice, pool)
    _engine(db, cat.ENGINE_OWN)
    return client


@pytest.fixture
def voice() -> TextVoice:
    return TextVoice()


def _start(client: TestClient, user: str = "alice", **body):
    return client.post("/v1/voice/sessions", json=body, headers=auth_header(user))


def _turn(client: TestClient, session_id: str, text: str, user: str = "alice"):
    return client.post(f"/v1/voice/sessions/{session_id}/turns", json={"text": text}, headers=auth_header(user))


def test_own_speech_with_no_speech_machine_is_text_and_has_no_sdp(db, tokens, group_map, objects, voice):
    pool = FakePool("no_voice_account_registered")
    client = _text_app(db, tokens, group_map, objects, voice, pool)

    response = _start(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "text" and body["state"] == vs.LIVE
    assert body["fallback"] == {"from": cat.ENGINE_OWN, "reason": vs.FALLBACK_NOT_DEPLOYED}
    assert "answer_sdp" not in body
    assert pool.asked == []  # own speech never asks the OpenAI pool
    (call,) = voice.starts
    assert call.mode == vs.MODE_TEXT and call.offer == ""
    (session,) = _sessions(db).values()
    assert session["mode"] == "text" and session["state"] == vs.LIVE
    # An offer a console sends anyway is dropped unread.
    _end(client, "alice", body["session_id"])
    again = _start(client, offer_sdp=OFFER)
    assert again.status_code == 200 and again.json()["mode"] == "text"
    assert voice.starts[-1].offer == ""


def test_gpt_live_with_no_api_key_in_the_pool_is_refused_not_text(db, tokens, group_map, objects, voice):
    pool = FakePool("no_voice_account_registered")
    client = _app(db, tokens, group_map, objects, voice, pool)

    response = _start(client, offer_sdp=OFFER)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert response.json()["detail"]["reason"] == vs.NO_OPENAI_KEY
    assert voice.starts == [] and _sessions(db) == {}


def test_with_an_api_key_the_session_is_live_as_today(db, tokens, group_map, objects, voice):
    client = _app(db, tokens, group_map, objects, voice, FakePool(""))

    missing = _start(client)
    assert missing.status_code == 422 and missing.json()["detail"]["field"] == "offer_sdp"
    assert voice.starts == []

    response = _start(client, offer_sdp=OFFER)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "live" and response.json()["answer_sdp"] == ANSWER
    assert voice.starts[0].mode == vs.MODE_LIVE and voice.starts[0].offer == OFFER


@pytest.mark.parametrize("pool", [FakePool("not_in_voice_pool"), FakePool("no_voice_account_available"),
                                  BrokenPool()])
def test_any_other_pool_answer_keeps_the_live_path(db, tokens, group_map, objects, voice, pool):
    client = _app(db, tokens, group_map, objects, voice, pool)
    response = _start(client, offer_sdp=OFFER)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "live"


def test_a_turn_returns_the_reply_and_cards_and_never_logs_the_text(
        db, tokens, group_map, objects, voice, caplog):
    caplog.set_level(logging.DEBUG)
    client = _text_app(db, tokens, group_map, objects, voice, FakePool("no_voice_account_registered"))
    session_id = _start(client).json()["session_id"]
    said = "plan the rollout for the payments team " + secrets.token_hex(4)

    response = _turn(client, session_id, said)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"turn_id", "reply", "cards"}
    assert body["reply"] == "Three steps are on screen."
    assert body["cards"] == [{"kind": "plan", "card_id": "card_1", "title": "Rollout"}]
    assert voice.turns == [(session_id, said)]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert said not in logged and "Three steps" not in logged
    # Nothing stores the text either.
    assert said not in json.dumps({k: str(v) for k, v in db.docs.items()})


def test_a_turn_is_409_on_a_live_session_and_after_the_end(db, tokens, group_map, objects, voice):
    client = _app(db, tokens, group_map, objects, voice, FakePool(""))
    live_id = _start(client, offer_sdp=OFFER).json()["session_id"]
    refused = _turn(client, live_id, "hello")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "conflict"
    assert refused.json()["detail"]["reason"] == vs.NOT_LIVE
    _end(client, "alice", live_id)

    _engine(db, cat.ENGINE_OWN)
    text_id = _start(client).json()["session_id"]
    assert _turn(client, text_id, "hello").status_code == 200
    _end(client, "alice", text_id)
    after = _turn(client, text_id, "hello again")
    assert after.status_code == 409 and after.json()["detail"]["reason"] == vs.NOT_LIVE
    assert len(voice.turns) == 1


def test_a_turn_passes_every_voice_it_guard(db, tokens, group_map, objects, voice):
    client = _text_app(db, tokens, group_map, objects, voice, FakePool("no_voice_account_registered"))
    session_id = _start(client).json()["session_id"]

    assert _turn(client, session_id, "hi", user="carol").status_code == 403  # not on the Voice it list
    assert _turn(client, session_id, "hi", user="bob").status_code == 404  # another person's, another tenant
    assert _turn(client, "vs_" + secrets.token_hex(16), "hi").status_code == 404
    assert _turn(client, "not-an-id", "hi").status_code == 422
    assert _turn(client, session_id, "x" * 4001).status_code == 422
    assert _turn(client, session_id, "").status_code == 422
    assert voice.turns == []


def test_the_plans_usage_limit_is_429_with_when_it_resets(db, tokens, group_map, objects, voice):
    client = _text_app(db, tokens, group_map, objects, voice, FakePool("no_voice_account_registered"))
    session_id = _start(client).json()["session_id"]
    voice.turn_error = vs.VoiceUsageLimited("2100-01-01T00:00:00+00:00", now=datetime.now(timezone.utc))

    response = _turn(client, session_id, "hello")

    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "rate_limited"
    assert body["detail"]["reason"] == "usage_limit_reached"
    assert body["detail"]["resets_at"] == "2100-01-01T00:00:00+00:00"
    assert body["detail"]["retry_after_seconds"] > 0


def _http_client(monkeypatch, answers: list[tuple[int, dict]]) -> tuple[vs.HttpVoiceClient, list]:
    sent: list = []
    client = vs.HttpVoiceClient("http://voice.internal:8080", token=lambda audience, timeout: "idt")

    def post(path, body, timeout):
        sent.append((path, body))
        status, payload = answers.pop(0)
        return status, json.dumps(payload).encode()

    monkeypatch.setattr(client, "_post", post)
    return client, sent


def test_the_http_client_starts_a_text_session_with_no_offer(monkeypatch):
    session_id = "vs_" + secrets.token_hex(16)
    client, sent = _http_client(monkeypatch, [(201, {"voice_session_id": session_id, "mode": "text"})])
    grant = secrets.token_urlsafe(32)
    call = vs.StartCall(session_id=session_id, project_id="vp_" + secrets.token_hex(16), tenant_id="eng",
                        person="alice@example.com", offer="", grant=grant, mode=vs.MODE_TEXT)

    assert client.start(call) == vs.StartAnswer(answer="", live_session_id="", mode=vs.MODE_TEXT)
    ((path, body),) = sent
    assert path == "/internal/sessions"
    assert body == {"voice_session_id": session_id, "project_id": call.project_id,
                    "person": "alice@example.com", "grant": grant, "mode": "text"}
    from swarm_voice.main import StartRequest
    assert set(body) <= set(StartRequest.model_fields)


def test_the_http_client_reads_swarm_voices_turn_answers(monkeypatch):
    session_id = "vs_" + secrets.token_hex(16)
    client, sent = _http_client(monkeypatch, [
        (200, {"turn_id": "vt_1", "reply": "Sure.", "cards": [{"kind": "plan"}, "junk"]}),
        (429, {"code": "usage_limit_reached", "message": "limit", "resets_at": "2100-01-01T00:00:00+00:00"}),
        (404, {"detail": "no such text conversation"}),
        (500, {}),
    ])
    assert client.turn(session_id, "hi") == vs.TurnAnswer(turn_id="vt_1", reply="Sure.", cards=[{"kind": "plan"}])
    assert sent[0] == (f"/internal/sessions/{session_id}/turns", {"text": "hi"})
    with pytest.raises(vs.VoiceUsageLimited) as limited:
        client.turn(session_id, "hi")
    assert limited.value.detail["resets_at"] == "2100-01-01T00:00:00+00:00"
    with pytest.raises(vs.VoiceSessionNotLive):
        client.turn(session_id, "hi")
    with pytest.raises(vs.VoiceUnreachable):
        client.turn(session_id, "hi")
